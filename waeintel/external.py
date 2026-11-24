"""External HTTP sources: Contest Online Scoreboard, solar indices, HamQTH lookups."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import xml.etree.ElementTree as ET

import aiohttp

log = logging.getLogger("external")
UA = {"User-Agent": "Mozilla/5.0 (compatible; WAE-RTTY-Intel/1.0; +https://github.com/aljaz2008/wae-rtty-software)"}


def _strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


# ---------------------------------------------------------------- scoreboard
NUM_KEYS = {
    "score": ("score", "totalscore", "claimed", "points_total", "total"),
    "qsos": ("qsos", "qso", "totalqsos", "q"),
    "mults": ("mults", "mult", "multipliers", "totalmults"),
    "qtcs": ("qtcs", "qtc"),
}


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _rows_from_json(obj):
    """Walk JSON and yield dicts that look like station rows (have a call field)."""
    if isinstance(obj, dict):
        keys = {k.lower(): k for k in obj}
        if any(k in keys for k in ("call", "callsign", "station")):
            yield obj
        for v in obj.values():
            yield from _rows_from_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _rows_from_json(v)


def _normalise(row: dict):
    low = {str(k).lower(): v for k, v in row.items()}
    call = str(low.get("call") or low.get("callsign") or low.get("station") or "").upper().strip()
    out = {"call": call}
    cats = [str(v) for k, v in low.items() if ("cat" in k or k in ("class", "power", "overlay"))
            and isinstance(v, (str, int)) and str(v).strip()]
    if cats:
        out["category"] = " ".join(cats)
    for k, names in NUM_KEYS.items():
        for n in names:
            if n in low and _num(low[n]) is not None:
                out[k] = _num(low[n])
                break
    # band breakdown: keys like '80', '80m', 'qso80', 'b80'
    bands = {}
    for k, v in low.items():
        m = re.fullmatch(r"(?:b|qsos?_?|band_?)?(160|80|40|20|15|10)m?", k)
        if m and _num(v) is not None:
            bands[m.group(1)] = _num(v)
        if isinstance(v, dict) and re.fullmatch(r"(160|80|40|20|15|10)m?", k):
            q = _num(v.get("qsos") or v.get("qso") or v.get("q"))
            if q is not None:
                bands[k.rstrip("m")] = q
    nested = low.get("bands") or low.get("band")
    if isinstance(nested, dict):
        for k, v in nested.items():
            kb = str(k).lower().rstrip("m")
            q = _num(v) if not isinstance(v, dict) else _num(v.get("qsos") or v.get("qso") or v.get("q"))
            if kb in ("160", "80", "40", "20", "15", "10") and q is not None:
                bands[kb] = q
    if bands:
        out["bands"] = bands
    return out


# ---------------------------------------------------------------- HTML scoreboards
from html.parser import HTMLParser  # noqa: E402

CALL_RE = re.compile(r"^(?=.*\d)(?=.*[A-Z])[A-Z0-9]{1,4}\d[A-Z0-9]{0,5}(/[A-Z0-9]{1,4})*$")
OPCALL_RE = re.compile(r"^\s*([A-Z0-9/]+)\s*(?:\(\s*([A-Z0-9/]+)\s*\))?\s*$")
COLWORDS = {"SCORE", "QSO", "QSOS", "COUNTRIES", "MULTS", "MULT", "LAST", "LOG", "CALL", "CALLSIGN",
            "RANK", "#", "CLUB", "POINTS", "TIME", "QTC", "QTCS"}


class _TableRows(HTMLParser):
    """Collects table rows as lists of (text, is_th, href) cells, plus headings between tables."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self._row, self._cell, self._th, self._href = [], None, None, False, None
        self._head = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell, self._th, self._href = [], tag == "th", None
        elif tag == "a" and self._cell is not None and a.get("href"):
            self._href = self._href or a["href"]
        elif tag in ("h1", "h2", "h3", "h4", "caption") and self._row is None:
            self._head = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append((" ".join("".join(self._cell).split()), self._th, self._href))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(("row", self._row))
            self._row = None
        elif tag in ("h1", "h2", "h3", "h4", "caption") and self._head is not None:
            t = " ".join("".join(self._head).split())
            if t:
                self.rows.append(("head", t))
            self._head = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        elif self._head is not None:
            self._head.append(data)


def _int(s):
    s = s.replace(",", "").replace(".", "").replace(" ", "").replace("\u00a0", "")
    return int(s) if s.isdigit() else None


def parse_html_scoreboard(text: str):
    """contestonlinescore.com style: one table/section per category, header row starts with the
    category (e.g. 'M/S HP  Score QSO Countries Last Log'), data rows 'rank call score qso mults hh:mm club'."""
    p = _TableRows()
    p.feed(text)
    out, category = [], ""
    for kind, item in p.rows:
        if kind == "head":
            if not any(w in item.upper().split() for w in COLWORDS):
                category = item
            continue
        cells = item
        texts = [c[0] for c in cells]
        upper = [t.upper() for t in texts]
        if any(c[1] for c in cells) or (sum(1 for t in upper if t in COLWORDS) >= 2):
            lab = [t for t in texts if t and t.upper() not in COLWORDS]
            if lab:
                category = lab[0]
            continue
        # find the callsign cell (prefer a ?call= link). Cells may read "S51A (S58Q)" = station (operator)
        def split_cs(t):
            m = OPCALL_RE.match(t)
            if m and CALL_RE.match(m.group(1)):
                return m.group(1), m.group(2)
            return None
        ci = None
        for i, (t, _th, href) in enumerate(cells):
            if href and "call=" in href.lower() and split_cs(t.upper()):
                ci = i
                break
        if ci is None:
            ci = next((i for i, t in enumerate(upper) if split_cs(t)), None)
        if ci is None:
            continue
        call, op = split_cs(upper[ci])
        nums = []
        last = ""
        for t in texts[ci + 1:]:
            if re.fullmatch(r"\d{1,2}:\d{2}", t):
                last = t
                break
            v = _int(t)
            if v is None:
                break
            nums.append(v)
        if not nums:
            continue
        row = {"call": call, "score": float(nums[0]), "category": category}
        if op:
            row["op"] = op
        if len(nums) > 1:
            row["qsos"] = float(nums[1])
        if len(nums) > 2:
            row["mults"] = float(nums[2])
        if last:
            row["last"] = last
        out.append(row)
    return out


def parse_scoreboard(text: str):
    text = text.strip()
    rows = []
    head = text[:2000].lower()
    if "<html" in head or "<!doctype" in head or "<table" in text[:200000].lower():
        rows = parse_html_scoreboard(text)
    elif text[:1] in "[{":
        rows = [_normalise(r) for r in _rows_from_json(json.loads(text))]
    elif text.startswith("<"):
        root = _strip_ns(ET.fromstring(text))
        for el in root.iter():
            d = {c.tag: (c.text or "") for c in el}
            d.update(el.attrib)
            if any(k.lower() in ("call", "callsign") for k in d):
                rows.append(_normalise(d))
    best = {}
    for r in rows:
        if not r.get("call"):
            continue
        b = best.get(r["call"])
        if b is None:
            best[r["call"]] = r
            continue
        cats = [c for c in (b.get("category", ""), r.get("category", "")) if c]
        if len(r) > len(b):
            best[r["call"]] = b = dict(r)
        if cats:
            b["category"] = " | ".join(dict.fromkeys(" | ".join(cats).split(" | ")))
    return list(best.values())


class Scoreboard:
    def __init__(self, url, every, rivals):
        self.url, self.every = url, every
        self.rivals = {r.upper() for r in rivals}
        self.history: dict[str, list] = {}   # call -> [(ts, row)]  (all stations)
        self.last_rows: list = []
        self.last_ts = 0.0
        self.fetch_ms = None
        self.fetches = 0
        self.status = "disabled" if not url else "waiting"

    async def run(self, session):
        if not self.url:
            return
        while True:
            try:
                t0 = time.perf_counter()
                async with session.get(self.url, headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    text = await r.text()
                rows = parse_scoreboard(text)
                self.fetch_ms = round((time.perf_counter() - t0) * 1000)
                self.fetches += 1
                now = time.time()
                for row in rows:
                    h = self.history.setdefault(row["call"], [])
                    h.append((now, row))
                    del h[:-200]
                self.last_rows, self.last_ts = rows, now
                self.status = f"ok, {len(rows)} rows @ {time.strftime('%H:%M:%S', time.gmtime(now))}Z"
            except Exception as e:  # noqa: BLE001
                self.status = f"error: {type(e).__name__}: {e}"[:160]
            await asyncio.sleep(self.every)

    def score_rate(self, call, mins=30):
        """Score points per hour from history (uses the sample closest to `mins` ago, >= 10 min)."""
        h = self.history.get(call.upper())
        if not h or "score" not in h[-1][1]:
            return None
        now, last = h[-1]
        older = [x for x in h if x[0] <= now - 600 and "score" in x[1]]
        if not older:
            return None
        t0, r0 = min(older, key=lambda x: abs(x[0] - (now - mins * 60)))
        return (last["score"] - r0["score"]) * 3600 / (now - t0)

    def summary(self, call):
        h = self.history.get(call.upper())
        if not h:
            return None
        now, last = h[-1]
        out = dict(last)

        def rate(mins):
            older = [x for x in h if x[0] <= now - mins * 60]
            if not older or "qsos" not in last:
                return None
            t0, r0 = older[-1]
            if "qsos" not in r0 or now - t0 < 60:
                return None
            return round((last["qsos"] - r0["qsos"]) * 3600 / (now - t0))

        out["rate10"] = rate(10)
        out["rate60"] = rate(60)
        out["age_s"] = int(time.time() - now)
        # which band grew most in last ~10 min
        if "bands" in last:
            older = [x for x in h if x[0] <= now - 600 and "bands" in x[1]]
            if older:
                b0 = older[-1][1]["bands"]
                diffs = {b: last["bands"][b] - b0.get(b, 0) for b in last["bands"]}
                best = max(diffs, key=diffs.get)
                if diffs[best] > 0:
                    out["active_band"] = best
        return out


# ---------------------------------------------------------------- solar
async def solar_poller(session, state, url, every=1800):
    while True:
        try:
            async with session.get(url, headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
            root = _strip_ns(ET.fromstring(text))
            sd = root.find(".//solardata")
            g = (lambda k: (sd.findtext(k) or "").strip()) if sd is not None else (lambda k: "")
            state.solar = {"sfi": g("solarflux"), "a": g("aindex"), "k": g("kindex"),
                           "ssn": g("sunspots"), "xray": g("xray"), "updated": g("updated")}
            state.status["solar"] = "ok"
        except Exception as e:  # noqa: BLE001
            state.status["solar"] = f"error: {e}"[:120]
        await asyncio.sleep(every)


# ---------------------------------------------------------------- HamQTH
class HamQTH:
    BASE = "https://www.hamqth.com/xml.php"

    def __init__(self, user, password):
        self.user, self.password = user, password
        self.sid = None
        self.sid_ts = 0
        self.cache: dict[str, dict | None] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.pending = set()
        self.enabled = bool(user and password)
        self.status = "disabled" if not self.enabled else "idle"

    def request(self, call):
        call = call.upper()
        if not self.enabled or call in self.cache or call in self.pending:
            return
        self.pending.add(call)
        self.queue.put_nowait(call)

    async def _login(self, session):
        async with session.get(self.BASE, params={"u": self.user, "p": self.password},
                               headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as r:
            root = _strip_ns(ET.fromstring(await r.text()))
        sid = root.findtext(".//session_id")
        if not sid:
            raise RuntimeError(root.findtext(".//error") or "login failed")
        self.sid, self.sid_ts = sid, time.time()

    async def run(self, session, on_result):
        if not self.enabled:
            return
        while True:
            call = await self.queue.get()
            try:
                if not self.sid or time.time() - self.sid_ts > 3000:
                    await self._login(session)
                async with session.get(self.BASE, params={"id": self.sid, "callsign": call, "prg": "WAEintel"},
                                       headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    root = _strip_ns(ET.fromstring(await r.text()))
                err = root.findtext(".//error")
                if err and "session" in err.lower():
                    self.sid = None
                    self.pending.discard(call)
                    self.request(call)
                    continue
                s = root.find(".//search")
                if s is None:
                    self.cache[call] = None
                else:
                    d = {c.tag: (c.text or "").strip() for c in s}
                    self.cache[call] = {
                        "name": d.get("nick") or d.get("adr_name", ""),
                        "qth": d.get("qth", ""), "country": d.get("country", ""),
                        "grid": d.get("grid", ""), "lat": _num(d.get("latitude")),
                        "lon": _num(d.get("longitude")), "itu": d.get("itu", ""), "cq": d.get("cq", ""),
                    }
                    on_result(call, self.cache[call])
                self.status = f"ok, {len(self.cache)} cached"
            except Exception as e:  # noqa: BLE001
                self.status = f"error: {e}"[:120]
                self.cache.setdefault(call, None)
            finally:
                self.pending.discard(call)
            await asyncio.sleep(1.0)  # be polite
