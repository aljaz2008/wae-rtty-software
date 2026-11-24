"""N1MM Logger+ integration: UDP broadcasts (contacts + radio) and optional read-only DB import."""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .spots import freq_to_band

log = logging.getLogger("n1mm")


def _parse_ts(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def _band_from(fields):
    for k in ("rxfreq", "txfreq"):
        v = fields.get(k)
        if v and v.strip("0. "):
            try:
                b = freq_to_band(float(v) / 100.0)  # N1MM sends 10 Hz units
                if b:
                    return b
            except ValueError:
                pass
    v = fields.get("band")
    try:
        mhz = float(v)
        return {1.8: 160, 3.5: 80, 7: 40, 14: 20, 21: 15, 28: 10}.get(mhz)
    except (TypeError, ValueError):
        return None


class N1MMProtocol(asyncio.DatagramProtocol):
    def __init__(self, state):
        self.state = state
        self.packets = 0

    def datagram_received(self, data, addr):
        self.packets += 1
        self.state.perf["udp_packets"] = self.packets
        try:
            root = ET.fromstring(data.decode("utf-8", "replace").strip("\x00 \r\n"))
        except ET.ParseError:
            return
        tag = root.tag.lower()
        f = {c.tag.lower(): (c.text or "").strip() for c in root}
        if tag in ("contactinfo", "contactreplace"):
            call = f.get("call", "").upper()
            if not call:
                return
            qid = f.get("id") or f"{call}-{f.get('timestamp')}"
            self.state.upsert_qso(qid, call, _band_from(f), _parse_ts(f.get("timestamp")),
                                  f.get("mode", ""))
        elif tag == "contactdelete":
            self.state.delete_qso(f.get("id") or "")
        elif tag == "radioinfo":
            try:
                khz = float(f.get("freq", "0")) / 100.0
            except ValueError:
                khz = 0
            active = f.get("activeradionr") or f.get("radionr")
            if f.get("radionr") == active or not active:
                self.state.radio = {"khz": khz, "band": freq_to_band(khz), "mode": f.get("mode", ""),
                                    "nr": f.get("radionr") or active or "1",
                                    "tx": f.get("istransmitting", "").lower() == "true"}


async def start_udp(state, host, port):
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        lambda: N1MMProtocol(state), local_addr=(host, port))
    log.info("N1MM UDP listening on %s:%s", host, port)
    return proto


def candidate_dbs():
    home = os.path.expanduser("~")
    pats = [os.path.join(home, "Documents", "N1MM Logger+", "Databases", "*.s3db"),
            os.path.join(home, "OneDrive", "Documents", "N1MM Logger+", "Databases", "*.s3db"),
            os.path.join(home, "My Documents", "N1MM Logger+", "Databases", "*.s3db")]
    return [f for p in pats for f in glob.glob(p)]


def _wae_rows(path, start_ts, end_ts, contest_like):
    """Number of QSOs of this contest inside the window, or -1 if unreadable."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute('SELECT TS FROM DXLOG WHERE ContestName LIKE ?', (contest_like,)).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return -1
    n = 0
    for (ts,) in rows:
        t = _parse_ts(ts)
        if t and start_ts <= t <= end_ts:
            n += 1
    return n


_auto_cache = {"t": 0.0, "path": None}


def find_db(path_cfg, start_ts=None, end_ts=None, contest_like="WAE%"):
    """Explicit path -> itself. 'auto' -> the .s3db in N1MM's Databases folder(s) that holds the most
    QSOs of this contest inside the contest window; newest file if no window is given or none match.
    (Plain 'newest file' picked the wrong database at a club station with many contest logs.)"""
    if path_cfg and path_cfg.lower() != "auto":
        return path_cfg
    files = candidate_dbs()
    if not files:
        return None
    if start_ts is None:
        return max(files, key=os.path.getmtime)
    if _auto_cache["path"] and time.time() - _auto_cache["t"] < 300:
        return _auto_cache["path"]
    scored = sorted(((_wae_rows(f, start_ts, end_ts, contest_like), os.path.getmtime(f), f) for f in files),
                    reverse=True)
    best = scored[0][2] if scored[0][0] > 0 else max(files, key=os.path.getmtime)
    _auto_cache.update(t=time.time(), path=best)
    return best


def _cols(con, table):
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def _pick(cols, exact=(), contains=(), exclude=()):
    low = {c.lower(): c for c in cols}
    for e in exact:
        if e in low:
            return low[e]
    for c in cols:
        cl = c.lower()
        if any(k in cl for k in contains) and not any(x in cl for x in exclude):
            return c
    return None


def _truthy(v):
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("1", "true", "yes", "y", "t")


class N1MMDB:
    """Read-only reader for N1MM Logger+ .s3db: QSOs from DXLOG, QTCs from a QTC table (auto-detected)."""

    def __init__(self, path_cfg, start_ts, end_ts, contest_like="WAE%", qtc_cfg=None, copy_mode=False):
        self.path_cfg = path_cfg
        self.copy_mode = copy_mode
        self.start, self.end = start_ts, end_ts
        self.contest_like = contest_like
        self.qtc_cfg = qtc_cfg or {}
        self.mapping = {}
        self.path = None

    def _connect(self):
        self.path = find_db(self.path_cfg, self.start, self.end, self.contest_like)
        if not self.path or not os.path.exists(self.path):
            raise FileNotFoundError(f"N1MM database not found ({self.path_cfg})")
        src = self.path
        if self.copy_mode:      # read a snapshot copy: never takes a lock on N1MM's live file
            import shutil
            import tempfile
            dst = os.path.join(tempfile.gettempdir(), "wae_intel_n1mm_copy.s3db")
            shutil.copyfile(src, dst)
            src = dst
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=1)
        con.execute("PRAGMA query_only=1")
        return con

    def _discover(self, con):
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        dx = next((t for t in tables if t.lower() == "dxlog"), None)
        if not dx:
            raise RuntimeError(f"no DXLOG table; tables: {tables}")
        c = _cols(con, dx)
        m = {"dxlog": dx,
             "call": _pick(c, ("call",)), "band": _pick(c, ("band",)), "ts": _pick(c, ("ts",)),
             "mode": _pick(c, ("mode",)), "contest": _pick(c, ("contestname",)),
             "contestnr": _pick(c, ("contestnr",))}
        if not (m["call"] and m["band"] and m["ts"]):
            raise RuntimeError(f"DXLOG columns not recognised: {c}")
        qc = self.qtc_cfg
        qt = qc.get("table") or next((t for t in tables if "qtc" in t.lower()), None)
        m["qtc_table"] = qt
        if qt:
            qcols = _cols(con, qt)
            m["qtc_cols"] = qcols
            m["qtc_time"] = qc.get("time_col") or _pick(qcols, ("ts", "qtctime", "time", "timestamp"),
                                                          ("time", "date"))
            m["qtc_dir"] = qc.get("dir_col") or _pick(
                qcols, (), ("rcv", "recv", "receiv", "sent", "direction", "isqtc", "qtctype"),
                ("call", "time", "nr", "serial", "freq"))
            m["qtc_contestnr"] = _pick(qcols, ("contestnr",))
            m["qtc_callcols"] = [qc["call_col"]] if qc.get("call_col") else \
                [x for x in qcols if "call" in x.lower()]
        self.mapping = m
        return m

    def read(self):
        con = self._connect()
        try:
            m = self._discover(con)
            qc = lambda col, dflt: f'"{col}"' if col else dflt  # noqa: E731
            q = (f'SELECT {qc(m["call"], "NULL")}, {qc(m["band"], "NULL")}, {qc(m["ts"], "NULL")}, '
                 f'{qc(m["mode"], "NULL")}, {qc(m["contest"], "NULL")}, {qc(m["contestnr"], "0")} '
                 f'FROM "{m["dxlog"]}"')
            args = ()
            if m["contest"]:
                q += f' WHERE "{m["contest"]}" LIKE ?'
                args = (self.contest_like,)
            rows, nrs = [], set()
            for call, band, ts, mode, _cn, cnr in con.execute(q, args):
                t = _parse_ts(ts)
                if t is None or not (self.start <= t <= self.end):
                    continue
                try:
                    b = {1.8: 160, 3.5: 80, 7.0: 40, 14.0: 20, 21.0: 15, 28.0: 10}.get(float(band or 0))
                except ValueError:
                    b = None
                rows.append((f"db:{call}:{ts}", (call or "").upper(), b, t, mode or ""))
                nrs.add(cnr)
            qtc = self._read_qtc(con, m, nrs) if m.get("qtc_table") else None
            return rows, qtc
        finally:
            con.close()

    def _read_qtc(self, con, m, contest_nrs):
        cols = m["qtc_cols"]
        data = [dict(zip(cols, r)) for r in con.execute(f'SELECT * FROM "{m["qtc_table"]}"')]
        # restrict to this contest
        if m["qtc_contestnr"] and contest_nrs:
            data = [d for d in data if d.get(m["qtc_contestnr"]) in contest_nrs]
        elif m["qtc_time"]:
            data = [d for d in data if (lambda t: t and self.start <= t <= self.end)(_parse_ts(str(d.get(m["qtc_time"]) or "")))]
        # counterpart call column: the one whose values repeat (<=10 QTCs per station);
        # the column holding the *reported* QSO calls has ~1 row per value.
        cc = m["qtc_callcols"]
        if not cc:
            return {"by_call": {}, "events": [], "sent": 0, "rcvd": 0, "unknown": len(data),
                    "lines": len(data), "note": "no call column in QTC table"}
        if len(cc) > 1 and data:
            def ratio(col):
                vals = [str(d.get(col) or "").upper() for d in data if d.get(col)]
                return len(set(vals)) / max(1, len(vals))
            cc = sorted(cc, key=ratio)
        call_col = cc[0]
        m["qtc_call"] = call_col
        dcol = m.get("qtc_dir")
        by, events = {}, []
        sent = rcvd = unknown = 0
        for d in data:
            call = str(d.get(call_col) or "").upper().strip()
            if not call:
                continue
            e = by.setdefault(call, {"sent": 0, "rcvd": 0, "total": 0})
            e["total"] += 1
            direction = None
            if dcol:
                v = d.get(dcol)
                dl = dcol.lower()
                if any(k in dl for k in ("rcv", "recv", "receiv")):
                    direction = "rcvd" if _truthy(v) else "sent"
                elif "sent" in dl:
                    direction = "sent" if _truthy(v) else "rcvd"
                else:
                    sv = str(v or "").strip().upper()
                    if sv in ("S", "SENT", "TX", "SEND", "OUT"):
                        direction = "sent"
                    elif sv in ("R", "RCVD", "RX", "RECEIVED", "RCV", "IN"):
                        direction = "rcvd"
            if direction:
                e[direction] += 1
                if direction == "sent":
                    sent += 1
                else:
                    rcvd += 1
            else:
                unknown += 1
            if m.get("qtc_time"):
                t = _parse_ts(str(d.get(m["qtc_time"]) or ""))
                if t:
                    events.append(t)
        return {"by_call": by, "events": events, "sent": sent, "rcvd": rcvd, "unknown": unknown,
                "lines": len(data)}

    def describe(self):
        m = self.mapping
        if not m:
            return "not read yet"
        q = f"QTC table {m['qtc_table']} (station={m.get('qtc_call')}, dir={m.get('qtc_dir')}, time={m.get('qtc_time')})" \
            if m.get("qtc_table") else "no QTC table found"
        return f"{os.path.basename(self.path or '')}: {q}"


async def db_poller(state, db: N1MMDB, every=10):
    loop = asyncio.get_running_loop()
    while True:
        try:
            t0 = time.perf_counter()
            rows, qtc = await loop.run_in_executor(None, db.read)
            state.perf["db_ms"] = round((time.perf_counter() - t0) * 1000)
            state.sync_db(rows, qtc)
            ql = f", {qtc['lines']} QTC lines" if qtc else ""
            state.status["n1mm_db"] = f"ok, {len(rows)} QSOs{ql} · {db.describe()}"
        except sqlite3.OperationalError as e:      # locked while N1MM writes: retry soon
            state.status["n1mm_db"] = f"busy/retry: {e}"[:150]
        except Exception as e:  # noqa: BLE001
            state.status["n1mm_db"] = f"error: {e}"[:200]
        await asyncio.sleep(every)


def inspect_db(path_cfg, start_ts=0, end_ts=4e9, contest_like="%", qtc_cfg=None):
    """Print the DB schema + detected mapping: run `python wae_intel.py --inspect-db`."""
    db = N1MMDB(path_cfg, start_ts, end_ts, contest_like, qtc_cfg)
    con = db._connect()
    print("Database:", db.path)
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"\n== {t}  ({n} rows)\n   columns: {', '.join(_cols(con, t))}")
        if t.lower() == "dxlog" or "qtc" in t.lower():
            for r in con.execute(f'SELECT * FROM "{t}" ORDER BY rowid DESC LIMIT 3'):
                print("   sample:", r)
    con.close()
    try:
        rows, qtc = db.read()
        print("\nDetected mapping:", {k: v for k, v in db.mapping.items() if k != "qtc_cols"})
        print(f"{contest_like} QSOs found (all dates): {len(rows)}")
        if qtc:
            print(f"QTC lines: {qtc['lines']}  sent={qtc['sent']} rcvd={qtc['rcvd']} unknown-dir={qtc['unknown']}")
            for c, v in list(qtc["by_call"].items())[:10]:
                print("  ", c, v)
    except Exception as e:  # noqa: BLE001
        print("read failed:", e)
