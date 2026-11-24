"""Spot parsing and resilient async telnet clients for RBN / DX clusters."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

log = logging.getLogger("spots")

BANDS = [(1800, 2000, 160), (3500, 4000, 80), (7000, 7300, 40), (10100, 10150, 30),
         (14000, 14350, 20), (18068, 18168, 17), (21000, 21450, 15), (24890, 24990, 12),
         (28000, 29700, 10), (50000, 54000, 6)]


def freq_to_band(khz: float):
    for lo, hi, b in BANDS:
        if lo <= khz <= hi:
            return b
    return None


# FT8 dial frequencies + 3 kHz passband (kHz)
# (FT4 windows deliberately not excluded: contest RTTY overlaps them on 80/20 m)
FT_WINDOWS = [(3573, 3576), (7074, 7077), (14074, 14077), (21074, 21077), (28074, 28077)]

SPOT_RE = re.compile(
    r"^DX de\s+([A-Z0-9/#\-]+?)-?#?:?\s+(\d{3,6}(?:\.\d+)?)\s+([A-Z0-9/]+)\s+(.*?)\s*(\d{4})Z",
    re.I)
RBN_RE = re.compile(
    r"\b(CW|RTTY|PSK31|PSK63|FT8|FT4|BPSK31)\s+(-?\d+)\s*dB(?:\s+(\d+)\s*(WPM|BPS))?\s*(CQ|DX|BEACON|NCDXF B)?",
    re.I)


@dataclass
class Spot:
    ts: float
    spotter: str
    freq: float
    dx: str
    comment: str
    band: int | None
    mode: str | None
    snr: int | None
    is_cq: bool
    skimmer: bool
    source: str
    utc: str = ""


def clean_call(c: str) -> str:
    c = c.upper().strip()
    c = re.sub(r"-#$|-\d+$|#$|-$", "", c)
    return c


def parse_spot(line: str, source: str, rtty_segments, now=None):
    m = SPOT_RE.search(line.strip())
    if not m:
        return None
    spotter, freq, dx, comment, utc = m.groups()
    try:
        freq = float(freq)
    except ValueError:
        return None
    spotter = clean_call(spotter)
    dx = dx.upper()
    mode = snr = None
    is_cq = False
    skimmer = False
    rm = RBN_RE.search(comment)
    if rm:
        mode = rm.group(1).upper()
        snr = int(rm.group(2))
        is_cq = (rm.group(5) or "").upper() == "CQ"
        skimmer = True
    else:
        cu = comment.upper()
        if "RTTY" in cu or "RTY" in cu or "WAE" in cu:
            mode = "RTTY"
        elif re.search(r"\bCW\b", cu):
            mode = "CW"
        elif re.search(r"\b(FT8|FT4)\b", cu):
            mode = "FT8"
        elif re.search(r"\b(SSB|USB|LSB)\b", cu):
            mode = "SSB"
        if mode is None and any(lo <= freq <= hi for lo, hi in FT_WINDOWS):
            mode = "FT?"          # unlabeled spot on an FT8/FT4 watering hole: not a RTTY target
        if mode is None and any(lo <= freq <= hi for lo, hi in rtty_segments):
            mode = "RTTY?"
        if "CQ" in cu.split():
            is_cq = True
    return Spot(now or time.time(), spotter, freq, dx, comment.strip(), freq_to_band(freq),
                mode, snr, is_cq, skimmer, source, utc)


IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
PROMPTS = ("login", "call:", "callsign", "your call", "enter your", "please enter")


@dataclass
class FeedStatus:
    name: str
    host: str
    port: int
    state: str = "init"
    connected_since: float | None = None
    last_line: float | None = None
    spots: int = 0
    bytes: int = 0
    errors: int = 0
    last_error: str = ""


class TelnetFeed:
    def __init__(self, name, host, port, login, password, init_cmds, on_line, stale_s=600):
        self.name, self.host, self.port = name, host, int(port)
        self.login, self.password = login, password
        self.init_cmds = init_cmds or []
        self.on_line = on_line
        self.stale_s = stale_s
        self.status = FeedStatus(name, host, self.port)
        self._writer = None

    def _strip_iac(self, data: bytes, writer):
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (DO, DONT, WILL, WONT) and i + 2 < len(data):
                    opt = data[i + 2]
                    if cmd == DO:
                        writer.write(bytes([IAC, WONT, opt]))
                    elif cmd == WILL:
                        writer.write(bytes([IAC, DONT, opt]))
                    i += 3
                    continue
                if cmd == SB:
                    j = data.find(bytes([IAC, SE]), i)
                    i = j + 2 if j >= 0 else len(data)
                    continue
                if cmd == IAC:
                    out.append(IAC)
                i += 2
                continue
            out.append(b)
            i += 1
        return bytes(out)

    async def run(self):
        backoff = 5
        while True:
            try:
                self.status.state = "connecting"
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=20)
                self._writer = writer
                self.status.state = "login"
                self.status.connected_since = time.time()
                await self._session(reader, writer)
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.status.errors += 1
                self.status.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("%s: %s", self.name, self.status.last_error)
            finally:
                if self._writer:
                    try:
                        self._writer.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._writer = None
            self.status.state = f"retry in {backoff}s"
            self.status.connected_since = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    async def _session(self, reader, writer):
        buf = ""
        logged_in = False
        pw_sent = False
        t0 = time.time()
        while True:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=15)
            except asyncio.TimeoutError:
                data = None
            if data == b"":
                raise ConnectionError("closed by remote")
            if data:
                self.status.bytes += len(data)
                buf += self._strip_iac(data, writer).decode("latin-1", "replace")
                self.status.last_line = time.time()
            low = buf.lower()
            if not logged_in and (any(p in low for p in PROMPTS) or time.time() - t0 > 8):
                writer.write((self.login + "\r\n").encode())
                await writer.drain()
                logged_in = True
                self.status.state = "online"
                await asyncio.sleep(1.5)
                for c in self.init_cmds:
                    writer.write((c + "\r\n").encode())
                    await writer.drain()
                    await asyncio.sleep(0.5)
                buf = ""
                continue
            if logged_in and not pw_sent and self.password and "password" in low:
                writer.write((self.password + "\r\n").encode())
                await writer.drain()
                pw_sent = True
                buf = ""
                continue
            *lines, buf = re.split(r"\r\n|\n|\r", buf)
            for ln in lines:
                if ln.strip():
                    try:
                        if self.on_line(ln, self.name):
                            self.status.spots += 1
                    except Exception:  # noqa: BLE001
                        log.exception("on_line failed")
            if len(buf) > 8192:
                buf = buf[-1024:]
            if self.status.last_line and time.time() - self.status.last_line > self.stale_s:
                raise ConnectionError("stale feed")
