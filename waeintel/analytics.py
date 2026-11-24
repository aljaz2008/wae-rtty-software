"""Central state + WAE-specific analytics."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import statistics
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from .geo import (bearing_distance, circular_mean, compass, grid_to_latlon, solar_elevation,
                 sun_state, sunrise_sunset)
from .spots import parse_spot
from .sysstats import process_stats

log = logging.getLogger("analytics")
CONTEST_BANDS = (80, 40, 20, 15, 10)
WEIGHT = {80: 4, 40: 3, 20: 2, 15: 2, 10: 2}
# WAE §6: numerical call areas count as separate multipliers for these entities
AREA_MULT = {"K": "W", "VE": "VE", "VK": "VK", "ZL": "ZL", "ZS": "ZS", "JA": "JA",
             "BY": "BY", "PY": "PY", "UA9": "RA"}
_PFX_RE = re.compile(r"^([A-Z0-9]*?\d)(?=[A-Z]+$)")


def base_call(c: str):
    parts = [p for p in c.upper().split("/") if p]
    return max(parts, key=len) if parts else c.upper()


def area_digit(s: str):
    m = _PFX_RE.match(s)
    if m:
        return m.group(1)[-1]
    d = re.findall(r"\d", s)
    return d[-1] if d else None


class SpotRec:
    __slots__ = ("ts", "band", "kind", "sk", "dx", "brg", "dist", "snr", "sk_cont", "dx_cont")

    def __init__(self, ts, band, kind, sk, dx, brg, dist, snr, sk_cont, dx_cont):
        self.ts, self.band, self.kind, self.sk, self.dx = ts, band, kind, sk, dx
        self.brg, self.dist, self.snr, self.sk_cont, self.dx_cont = brg, dist, snr, sk_cont, dx_cont


class State:
    def __init__(self, cfg, cty):
        self.cfg = cfg
        self.cty = cty
        st = cfg["station"]
        self.mycall = st["call"].upper()
        # every call you may appear under on the scoreboard (station call, operator call, aliases)
        self.mycalls = {self.mycall} | {c.upper() for c in st.get("aliases", [])} | \
            ({st["login_call"].upper()} if st.get("login_call") else set())
        self.home = grid_to_latlon(st["grid"])
        a = cfg["analysis"]
        self.near_km = a.get("near_km", 1200)
        self.far_km = a.get("far_km", 2500)
        self.local_km = a.get("local_km", 400)     # skimmers this close = ground wave, ignored for direction
        self.window = a.get("window_min", 15) * 60
        self.sector = a.get("sector_deg", 30)
        self.alert_age = a.get("alert_max_age_min", 10) * 60
        self.rtty_only = a.get("rtty_only", True)
        self.prop_all_modes = a.get("propagation_all_modes", True)
        self.exp_points = a.get("expected_final_points", 2500)    # QSOs + QTCs
        self.exp_wmults = a.get("expected_final_wmults", 800)     # weighted mult sum
        self.qtc_value = a.get("qtc_value", 0.3)                  # one QTC vs one QSO, in operating time
        self.rtty_segments = [tuple(x) for x in a.get("rtty_segments", cfg.get("rtty_segments", []))]
        c = cfg["contest"]
        self.start = datetime.fromisoformat(c["start_utc"].replace("Z", "+00:00")).timestamp()
        self.end = datetime.fromisoformat(c["end_utc"].replace("Z", "+00:00")).timestamp()
        self.max_op_h = c.get("max_operating_hours", 36)
        self.off_min = c.get("min_off_minutes", 60)

        self.spots: deque = deque()        # SpotRec, propagation-relevant only
        self.dedupe: dict = {}
        self.latest: dict = {}             # (dx, band) -> dict   RTTY targets
        self.qsos: dict = {}               # id -> (call, band, ts, mode)
        self.qso_keys: dict = {}
        self.db_qtc = {"by_call": {}, "events": [], "sent": 0, "rcvd": 0, "unknown": 0, "lines": 0}
        self.radio = {}
        self.solar = {}
        self.status = {}
        self.feeds = []
        self.scoreboard = None
        self.hamqth = None
        rc = cfg.get("rivals", {})
        self.rivals_manual = [r.upper() for r in rc.get("calls", [])]
        self.rivals_auto_cfg = (rc.get("auto", True), rc.get("auto_top", 5), rc.get("auto_around", 3))
        self.rivals = list(self.rivals_manual)
        self.rivals_auto: list = []
        self._rivals_ts = 0.0
        self.qtc_path = cfg.get("files", {}).get("qtc_ledger", "qtc_ledger.json")
        self.qtc = self._load_qtc()
        self.loc_override: dict = {}
        self._where_cache: dict = {}
        self._load_skimmers(cfg.get("files", {}).get("skimmers_csv"))
        self.alerts_seen: set = set()
        self.spot_count = 0
        self.ingested = 0
        self.lines_in = 0
        self.in_times: deque = deque()          # ts of accepted spots (last 5 min) for rate
        self.hist: deque = deque(maxlen=4000)   # (ts, points, wmults, score) sampled each minute
        self.started = time.time()
        sb = cfg.get("scoreboard", {})
        self.sb_category = sb.get("category", "")
        self.duty = sb.get("rival_duty_cycle", 0.75)
        self.perf = {"snap_ms": [], "lag_ms": [], "db_ms": None, "ws_clients": 0, "http_snapshots": 0,
                     "udp_packets": 0}

    # ------------------------------------------------------------ persistence
    def _load_qtc(self):
        try:
            with open(self.qtc_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"stations": {}, "events": []}

    def _save_qtc(self):
        tmp = self.qtc_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.qtc, f, indent=1)
        os.replace(tmp, self.qtc_path)

    def qtc_update(self, call, sent=0, rcvd=0):
        """Manual QTC ledger (corrections / when the N1MM DB has no QTC data)."""
        call = call.upper().strip()
        s = self.qtc["stations"].setdefault(call, {"sent": 0, "rcvd": 0})
        s["sent"] = max(0, s["sent"] + int(sent))
        s["rcvd"] = max(0, s["rcvd"] + int(rcvd))
        self.qtc["events"].append([time.time(), call, int(sent), int(rcvd)])
        self._save_qtc()
        return s

    def qtc_for(self, call):
        """Combined QTC count with a station: N1MM DB + manual ledger."""
        bc = base_call(call)
        d = self.db_qtc["by_call"].get(bc, {})
        m = self.qtc["stations"].get(bc, {})
        sent = d.get("sent", 0) + m.get("sent", 0)
        rcvd = d.get("rcvd", 0) + m.get("rcvd", 0)
        total = d.get("total", 0) + m.get("sent", 0) + m.get("rcvd", 0)
        return {"sent": sent, "rcvd": rcvd, "total": total}

    def qtc_totals(self):
        ms = sum(s["sent"] for s in self.qtc["stations"].values())
        mr = sum(s["rcvd"] for s in self.qtc["stations"].values())
        d = self.db_qtc
        return {"sent": d["sent"] + ms, "rcvd": d["rcvd"] + mr, "unknown_dir": d["unknown"],
                "total": d["lines"] + ms + mr}

    def _load_skimmers(self, path):
        if not path or not os.path.exists(path):
            return
        with open(path) as f:
            for line in f:
                p = [x.strip() for x in line.replace(";", ",").split(",")]
                if len(p) >= 2 and p[0] and not p[0].startswith("#"):
                    try:
                        self.loc_override[p[0].upper()] = grid_to_latlon(p[1])
                    except ValueError:
                        pass
        log.info("loaded %d skimmer locations", len(self.loc_override))

    def set_location(self, call, info):
        loc = None
        if info and info.get("lat") is not None and info.get("lon") is not None:
            loc = (info["lat"], info["lon"])
        elif info and info.get("grid"):
            try:
                loc = grid_to_latlon(info["grid"][:6])
            except ValueError:
                pass
        if loc:
            self.loc_override[call.upper()] = loc
            self._where_cache.clear()

    # ------------------------------------------------------------ geo + mults
    def mult_key(self, call, ci):
        """WAE multiplier id: entity, or entity+call area for W/VE/VK/ZL/ZS/JA/BY/PY/RA8-0."""
        pfx = ("*" if ci.entity.wae_only else "") + ci.entity.prefix
        lab = AREA_MULT.get(ci.entity.prefix)
        if not lab:
            return pfx
        sp = self.cty.split_call(call)
        if not sp:
            return pfx
        lk, base, area = sp
        d = area or area_digit(lk) or area_digit(base)
        if lab == "RA" and d not in ("8", "9", "0"):
            return pfx
        return f"{lab}{d}" if d else pfx

    def where(self, call):
        w = self._where_cache.get(call)
        if w is not None or call in self._where_cache:
            return w
        ci = self.cty.lookup(call)
        if ci is None:
            w = None
        else:
            lat, lon = self.loc_override.get(base_call(call), (ci.lat, ci.lon))
            brg, dist = bearing_distance(self.home[0], self.home[1], lat, lon)
            w = {"pfx": ("*" if ci.entity.wae_only else "") + ci.entity.prefix,
                 "mult": self.mult_key(call, ci),
                 "name": ci.entity.name, "cont": ci.cont, "cq": ci.cq,
                 "lat": lat, "lon": lon, "brg": brg, "dist": dist, "eu": ci.cont == "EU"}
        if len(self._where_cache) > 100000:
            self._where_cache.clear()
        self._where_cache[call] = w
        return w

    # ------------------------------------------------------------ ingest
    def on_line(self, line, source):
        self.lines_in += 1
        sp = parse_spot(line, source, self.rtty_segments)
        if sp is None or sp.band not in CONTEST_BANDS:
            return False
        is_rtty = sp.mode in ("RTTY", "RTTY?")
        if not is_rtty and not self.prop_all_modes:
            return False
        key = (sp.dx, sp.spotter, round(sp.freq))
        now = sp.ts
        last = self.dedupe.get(key)
        if last and now - last < 120:
            return False
        self.dedupe[key] = now
        self.ingested += 1
        self.in_times.append(now)

        # propagation record (only keep spots that say something about OUR paths)
        sk = self.where(sp.spotter)
        dx = self.where(sp.dx)
        if sk and dx:
            snr = sp.snr if sp.snr is not None else 10
            if base_call(sp.dx) == self.mycall:
                self.spots.append(SpotRec(now, sp.band, "me", sp.spotter, sp.dx, sk["brg"], sk["dist"],
                                          sp.snr, sk["cont"], dx["cont"]))
            elif sk["dist"] <= self.near_km and dx["dist"] > self.near_km:
                self.spots.append(SpotRec(now, sp.band, "hear", sp.spotter, base_call(sp.dx), dx["brg"],
                                          dx["dist"], snr, sk["cont"], dx["cont"]))
            elif dx["dist"] <= self.near_km and sk["dist"] >= self.far_km:
                self.spots.append(SpotRec(now, sp.band, "path", sp.spotter, base_call(sp.dx), sk["brg"],
                                          sk["dist"], snr, sk["cont"], dx["cont"]))
        self.spot_count += 1

        if self.rtty_only and not is_rtty:
            return True          # propagation evidence only, not a target
        k = (sp.dx, sp.band)
        L = self.latest.get(k)
        if L is None:
            L = self.latest[k] = {"dx": sp.dx, "band": sp.band, "first": now, "spotters": {},
                                  "freq": sp.freq, "cq": False, "human": False}
        L.update(last=now, freq=sp.freq)
        L["cq"] = sp.is_cq or (L["cq"] and now - L.get("cq_ts", 0) < 600)
        if sp.is_cq:
            L["cq_ts"] = now
        if not sp.skimmer:
            L["human"] = True
        L["spotters"][sp.spotter] = (now, sp.snr)
        if self.hamqth and base_call(sp.dx) in self.rivals:
            self.hamqth.request(base_call(sp.dx))
        if self.hamqth and self.cfg.get("hamqth", {}).get("lookup_skimmers") and sp.skimmer:
            if sp.spotter not in self.loc_override:
                self.hamqth.request(sp.spotter)
        return True

    def prune(self):
        now = time.time()
        cutoff = now - max(self.window, 900)
        while self.spots and self.spots[0].ts < cutoff:
            self.spots.popleft()
        if len(self.dedupe) > 50000:
            self.dedupe = {k: v for k, v in self.dedupe.items() if v > now - 300}
        old = now - 3600
        for k in [k for k, v in self.latest.items() if v["last"] < old]:
            del self.latest[k]

    # ------------------------------------------------------------ log
    def upsert_qso(self, qid, call, band, ts, mode):
        if not call or not band:
            return
        ts = ts or time.time()
        if not (self.start <= ts <= self.end):
            self.status["n1mm_udp_last"] = f"ignored {call} (outside contest window)"
            return
        self.status["n1mm_udp_last"] = f"{call} {band}m"
        key = (base_call(call), band, int(ts // 60))
        old = self.qso_keys.get(key)
        if old and old != qid:
            self.qsos.pop(old, None)
        prev = self.qsos.get(qid)
        if prev:
            self.qso_keys.pop((base_call(prev[0]), prev[1], int(prev[2] // 60)), None)
        self.qsos[qid] = (call.upper(), band, ts, mode)
        self.qso_keys[key] = qid

    def delete_qso(self, qid):
        prev = self.qsos.pop(qid, None)
        if prev:
            self.qso_keys.pop((base_call(prev[0]), prev[1], int(prev[2] // 60)), None)

    def sync_db(self, rows, qtc):
        """Authoritative log from N1MM DB: replaces QSOs, keeps very recent UDP-only QSOs."""
        if not rows and any(not k.startswith("db:") for k in self.qsos):
            # DB shows nothing for this contest but UDP delivered QSOs: wrong DB / contest name.
            self.status["n1mm_db_warn"] = "DB has 0 QSOs for this contest while UDP has some: check db_path/contest_like"
            if qtc is not None:
                self.db_qtc = qtc
            return
        self.status.pop("n1mm_db_warn", None)
        keys = set()
        new_qsos, new_keys = {}, {}
        for qid, call, band, ts, mode in rows:
            if not band or ts is None:
                continue
            key = (base_call(call), band, int(ts // 60))
            keys.add(key)
            new_qsos[qid] = (call.upper(), band, ts, mode)
            new_keys[key] = qid
        now = time.time()
        for qid, q in self.qsos.items():          # UDP QSO not in DB yet (< 2 min old)
            key = (base_call(q[0]), q[1], int(q[2] // 60))
            if key not in keys and not qid.startswith("db:") and now - q[2] < 120:
                new_qsos[qid] = q
                new_keys[key] = qid
        self.qsos, self.qso_keys = new_qsos, new_keys
        if qtc is not None:
            self.db_qtc = qtc

    def _worked(self):
        worked_cb, worked_call = set(), set()
        mults = defaultdict(set)
        eu_q = 0
        for call, band, ts, mode in self.qsos.values():
            b = base_call(call)
            worked_cb.add((b, band))
            worked_call.add(b)
            w = self.where(call)
            if w:
                mults[band].add(w["mult"])
                eu_q += w["eu"]
        return worked_cb, worked_call, mults, eu_q

    # ------------------------------------------------------------ operating time
    def op_time(self, now):
        acts = [q[2] for q in self.qsos.values()] + [e[0] for e in self.qtc["events"]] + \
            list(self.db_qtc.get("events", []))
        acts = sorted(t for t in acts if t and self.start <= t <= self.end)
        t_now = min(now, self.end)
        if t_now <= self.start:
            return {"used_h": 0, "left_h": self.max_op_h, "gap_min": 0, "gap_is_off": False,
                    "offs": [], "off_total_h": 0, "status": "not started"}
        off_s = self.off_min * 60
        pts = [self.start] + acts
        offs, off_total = [], 0.0
        for a, b in zip(pts, pts[1:]):
            if b - a >= off_s:
                offs.append((a, b))
                off_total += b - a
        gap = t_now - pts[-1]
        if gap >= off_s:
            off_total += gap
        used = 0 if not acts else (t_now - self.start) - off_total
        fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%a %H:%M")  # noqa: E731
        return {"used_h": round(used / 3600, 2), "left_h": round(self.max_op_h - used / 3600, 2),
                "gap_min": round(gap / 60, 1), "gap_is_off": gap >= off_s,
                "offs": [f"{fmt(a)}–{fmt(b)} ({(b - a) / 3600:.1f} h)" for a, b in offs],
                "off_total_h": round(off_total / 3600, 2),
                "status": "running" if now < self.end else "ended"}

    def my_rate(self, now):
        ts = [q[2] for q in self.qsos.values()]
        return {"r10": sum(1 for t in ts if t > now - 600) * 6,
                "r60": sum(1 for t in ts if t > now - 3600), "total": len(ts)}

    # ------------------------------------------------------------ audibility
    def audibility(self, L, w, band, cells, now, night_home):
        """0..1 estimate that YOU can work this station now, from live evidence."""
        for sk, (t, _snr) in L["spotters"].items():
            if now - t <= self.alert_age:
                ws = self.where(sk)
                if ws and ws["dist"] <= self.near_km:
                    return 1.0, "here"
        if w["dist"] <= self.near_km:
            if band == 80:
                return 0.6, "short"
            if band == 40:
                return (0.35, "short?") if night_home else (0.6, "short")
            skip_km = {20: 700, 15: 1000, 10: 1200}[band]
            return (0.1, "skip") if w["dist"] < skip_km else (0.4, "1-hop?")
        c = cells[int(w["brg"] // self.sector) % len(cells)]
        if c["me"]:
            return 0.7, "path"
        if c["score"] > 0:
            return round(0.25 + 0.35 * min(1.0, c["score"] / 4), 2), "path"
        return 0.05, "far"

    # ------------------------------------------------------------ snapshot
    def snapshot(self):
        t0 = time.perf_counter()
        snap = self._snapshot()
        now = time.time()
        sc = snap["score"]
        if not self.hist or now - self.hist[-1][0] >= 60:
            self.hist.append((now, sc["points"], snap["weighted_mults"], sc["est"]))
        snap["projection"] = self._projection(snap, now)
        snap["standings"] = self._standings(snap, now)
        ms = (time.perf_counter() - t0) * 1000
        self.perf["snap_ms"] = (self.perf["snap_ms"] + [ms])[-60:]
        snap["stats"] = self._stats(now, ms)
        return snap

    def _hist_at(self, t):
        older = [h for h in self.hist if h[0] <= t]
        return older[-1] if older else (self.hist[0] if self.hist else None)

    def _projection(self, snap, now):
        sc = snap["score"]
        P, W = sc["points"], snap["weighted_mults"]
        rem_contest = max(0.0, (self.end - now) / 3600)
        rem_op = max(0.0, min(snap["optime"]["left_h"], rem_contest))
        h60 = self._hist_at(now - 3600)
        if h60 and now - h60[0] >= 900:
            span = (now - h60[0]) / 3600
            p_rate = (P - h60[1]) / span
            w_rate = (W - h60[2]) / span
        else:
            p_rate, w_rate = snap["rate"]["r60"], 0.0
        # new mults get rarer: current mult rate decays with time constant tau (saturating)
        tau = self.cfg["analysis"].get("mult_decay_hours", 8.0)
        p_final = P + p_rate * rem_op
        w_final = W + max(0.0, w_rate) * tau * (1 - math.exp(-rem_op / tau))
        h30 = self._hist_at(now - 1800)
        score_rate = (sc["est"] - h30[3]) * 3600 / (now - h30[0]) if h30 and now - h30[0] >= 600 else None
        return {"now": sc["est"], "points": P, "wmults": W,
                "p_rate": round(p_rate, 1), "w_rate": round(w_rate, 1),
                "p_final": round(p_final), "w_final": round(w_final), "final": round(p_final * w_final),
                "rem_op_h": round(rem_op, 2), "rem_contest_h": round(rem_contest, 2),
                "score_rate": round(score_rate) if score_rate is not None else None,
                "expected_final": self.exp_points * self.exp_wmults,
                "confidence": "low (< 3 h operated)" if snap["optime"]["used_h"] < 3 else "ok"}

    @staticmethod
    def _cat_tokens(s):
        s = re.sub(r"(?<=\w)/(?=\w)", "", s.upper())          # S/O -> SO, M/S -> MS
        return {re.sub(r"^(160|80|40|20|15|10)M$", r"\1", t) for t in re.findall(r"[A-Z0-9]+", s)}

    def cat_match(self, row):
        if not self.sb_category:
            return True
        want = self._cat_tokens(self.sb_category)
        bands = {"160", "80", "40", "20", "15", "10"}
        for part in row.get("category", "").split(" | "):
            have = self._cat_tokens(part)
            if want <= have and not ((have & bands) and not (want & bands)):
                return True        # single-band classes only match if a band was asked for
        return False

    def _is_me(self, r):
        return r.get("call") in self.mycalls or r.get("op") in self.mycalls

    def _update_rivals(self):
        sb = self.scoreboard
        on, top, around = self.rivals_auto_cfg
        if not on or not sb or not sb.last_rows or sb.last_ts == self._rivals_ts:
            return
        self._rivals_ts = sb.last_ts
        rows = sorted((r for r in sb.last_rows if "score" in r and self.cat_match(r)), key=lambda r: -r["score"])
        calls = [self.mycall if self._is_me(r) else r["call"] for r in rows]
        auto = calls[:top]
        if self.mycall in calls:
            i = calls.index(self.mycall)
            auto += calls[max(0, i - around):i] + calls[i + 1:i + 1 + around]
        self.rivals_auto = [c for c in dict.fromkeys(auto) if c != self.mycall]
        self.rivals = list(dict.fromkeys(self.rivals_manual + self.rivals_auto))

    def _standings(self, snap, now):
        sb = self.scoreboard
        if not sb or not sb.last_rows:
            return {"available": False, "status": sb.status if sb else "disabled"}
        rows = [dict(r, call=self.mycall, _orig=r["call"]) if self._is_me(r) else r
                for r in sb.last_rows if "score" in r]
        rows = [r for r in rows if self.cat_match(r) or r["call"] == self.mycall]
        # a call can appear in several sections (e.g. overall + category): keep the first
        rows = list({r["call"]: r for r in reversed(rows)}.values())
        proj = snap["projection"]
        rem_c = proj["rem_contest_h"]
        mine_board = next((r for r in rows if r["call"] == self.mycall), None)
        my_score = mine_board["score"] if mine_board else proj["now"]
        my_rate = sb.score_rate(mine_board.get("_orig", self.mycall)) if mine_board else proj["score_rate"]
        table = []
        for r in rows:
            me = r["call"] == self.mycall
            rate = my_rate if me else sb.score_rate(r["call"])
            if me:
                # On the scoreboard: extend the *scoreboard* score with its own measured rate, so the
                # projection never starts from a different number than the ranking (WAE 2026 bug:
                # the local estimate lacked QTC points and projected below the score already made).
                if mine_board and rate is not None:
                    pf = r["score"] + max(0.0, rate) * proj["rem_op_h"]
                elif mine_board:
                    pf = max(r["score"], proj["final"])
                else:
                    pf = proj["final"]
            else:
                pf = r["score"] + (rate or 0) * rem_c * self.duty
            table.append({"call": r["call"], "score": r["score"], "qsos": r.get("qsos"),
                          "mults": r.get("mults"), "category": r.get("category", ""),
                          "rate": round(rate) if rate is not None else None, "proj": round(pf),
                          "me": me, "rival": r["call"] in self.rivals})
        if not mine_board:
            table.append({"call": self.mycall, "score": my_score, "qsos": snap["score"]["qsos"],
                          "mults": snap["weighted_mults"], "category": "(live est.)",
                          "rate": my_rate, "proj": proj["final"], "me": True, "rival": False})
        table.sort(key=lambda r: -r["score"])
        for i, r in enumerate(table):
            r["rank"] = i + 1
        by_proj = sorted(table, key=lambda r: -r["proj"])
        my_rank = next(r["rank"] for r in table if r["me"])
        my_proj_rank = next(i + 1 for i, r in enumerate(by_proj) if r["me"])
        P, W = max(1, proj["points"]), max(1, proj["wmults"])

        def need(gap, their_rate):
            gap = max(0, gap) + 1
            out = {"gap": round(gap), "qsos": round(gap / W), "m80": round(gap / (4 * P), 1),
                   "m40": round(gap / (3 * P), 1), "m20": round(gap / (2 * P), 1)}
            if my_rate is not None and their_rate is not None and my_rate > their_rate:
                out["catch_h"] = round(gap / (my_rate - their_rate), 1)
            return out

        targets = []
        if my_rank > 1:
            a = table[my_rank - 2]
            targets.append({"label": "next above now", "call": a["call"], "score": a["score"],
                            **need(a["score"] - my_score, a["rate"])})
        if my_proj_rank > 1:
            a = by_proj[my_proj_rank - 2]
            targets.append({"label": "next above at finish (projected)", "call": a["call"],
                            "proj": a["proj"], **need(a["proj"] - proj["final"], None)})
        for r in table:
            if r["rival"] and r["score"] > my_score:
                targets.append({"label": "rival", "call": r["call"], "score": r["score"],
                                **need(r["score"] - my_score, r["rate"])})
        # compact table: top 10 + my neighbourhood
        keep = {i for i in range(min(10, len(table)))} | \
            {i for i in range(max(0, my_rank - 4), min(len(table), my_rank + 3))} | \
            {i for i, r in enumerate(table) if r["rival"]}
        return {"available": True, "status": sb.status, "category": self.sb_category or "all",
                "auto_rivals": self.rivals_auto,
                "my_rank": my_rank, "of": len(table), "my_proj_rank": my_proj_rank,
                "my_score": my_score, "on_board": bool(mine_board), "duty": self.duty,
                "rows": [table[i] for i in sorted(keep)], "targets": targets}

    def _stats(self, now, snap_ms):
        while self.in_times and self.in_times[0] < now - 300:
            self.in_times.popleft()
        last60 = sum(1 for t in self.in_times if t > now - 60)
        sm = self.perf["snap_ms"]
        lag = self.perf["lag_ms"]
        feeds = []
        for f in self.feeds:
            st = f.status
            feeds.append({"name": st.name, "state": st.state, "spots": st.spots,
                          "kb": round(st.bytes / 1024), "reconnects": st.errors,
                          "up_min": round((now - st.connected_since) / 60) if st.connected_since else None})
        return {
            "uptime_min": round((now - self.started) / 60, 1),
            "proc": process_stats(),
            "snap_ms": round(snap_ms, 1), "snap_ms_avg": round(sum(sm) / len(sm), 1) if sm else None,
            "snap_ms_max": round(max(sm), 1) if sm else None,
            "loop_lag_ms": round(lag[-1], 1) if lag else None, "loop_lag_max": round(max(lag), 1) if lag else None,
            "spots_per_min": last60, "spots_per_min_5": round(len(self.in_times) / 5),
            "lines_in": self.lines_in, "ingested": self.ingested, "prop_kept": len(self.spots),
            "targets_live": len(self.latest), "where_cache": len(self._where_cache),
            "cty_cache": self.cty.lookup.cache_info().currsize,
            "qsos_loaded": len(self.qsos), "db_ms": self.perf["db_ms"],
            "udp_packets": self.perf["udp_packets"], "ws_clients": self.perf["ws_clients"],
            "http_snapshots": self.perf["http_snapshots"],
            "hamqth_queue": self.hamqth.queue.qsize() if self.hamqth else 0,
            "hamqth_cache": len(self.hamqth.cache) if self.hamqth else 0,
            "scoreboard_ms": self.scoreboard.fetch_ms if self.scoreboard else None,
            "scoreboard_age_s": round(now - self.scoreboard.last_ts) if self.scoreboard and self.scoreboard.last_ts else None,
            "feeds": feeds,
        }

    def _snapshot(self):
        now = time.time()
        self._update_rivals()
        self.prune()
        worked_cb, worked_call, mults, eu_q = self._worked()
        opt = self.op_time(now)
        qt = self.qtc_totals()
        nsec = 360 // self.sector
        night_home = solar_elevation(*self.home) < -6

        hear = {b: [defaultdict(set) for _ in range(nsec)] for b in CONTEST_BANDS}
        path = {b: [set() for _ in range(nsec)] for b in CONTEST_BANDS}
        me = {b: [dict() for _ in range(nsec)] for b in CONTEST_BANDS}
        brg_w = {b: [] for b in CONTEST_BANDS}
        snr_cell = defaultdict(list)
        cont_hear = {b: defaultdict(set) for b in CONTEST_BANDS}
        cont_path = {b: defaultdict(set) for b in CONTEST_BANDS}
        my_heard = []
        win_n = 0
        for s in self.spots:
            if s.ts <= now - self.window:
                continue
            win_n += 1
            b = s.band
            snr = s.snr if s.snr is not None else 10
            i = int(s.brg // self.sector) % nsec
            if s.kind == "me":
                my_heard.append({"t": s.ts, "band": b, "skimmer": s.sk, "snr": s.snr, "cont": s.sk_cont,
                                 "brg": round(s.brg), "km": round(s.dist)})
                if s.dist >= self.local_km:     # ground-wave neighbours say nothing about direction
                    prev = me[b][i].get(s.sk)
                    if prev is None or snr > prev:
                        me[b][i][s.sk] = snr
                    brg_w[b].append((s.brg, 2.0 * max(1, snr)))
            elif s.kind == "hear":
                hear[b][i][s.dx].add(s.sk)
                snr_cell[(b, i)].append(snr)
                brg_w[b].append((s.brg, max(1, snr)))
                cont_hear[b][s.dx_cont].add(s.dx)
            else:
                path[b][i].add(s.sk)
                brg_w[b].append((s.brg, 0.6 * max(1, snr)))
                cont_path[b][s.sk_cont].add(s.sk)

        live = defaultdict(list)
        for (dxc, band), L in self.latest.items():
            if now - L["last"] <= self.alert_age:
                live[band].append(L)

        # value of a new multiplier vs a new QSO, from (projected) final score
        points = len(worked_cb) + qt["total"]      # dupes score nothing
        wm = sum(WEIGHT[b] * len(mults[b]) for b in CONTEST_BANDS)
        if opt["used_h"] >= 3 and points:
            p_proj = max(points, points / opt["used_h"] * self.max_op_h)
        else:
            p_proj = max(points, self.exp_points)
        w_proj = max(wm, self.exp_wmults)
        mult_eq = {b: WEIGHT[b] * p_proj / w_proj for b in CONTEST_BANDS}   # QSO-equivalents

        bands_out = []
        for b in CONTEST_BANDS:
            cells = []
            for i in range(nsec):
                h, p, m = len(hear[b][i]), len(path[b][i]), len(me[b][i])
                score = h + 0.6 * p + 2.0 * m
                med = statistics.median(snr_cell[(b, i)]) if snr_cell[(b, i)] else None
                cells.append({"h": h, "p": p, "me": m, "score": round(score, 1), "snr": med,
                              "from": i * self.sector})
            best = max(cells, key=lambda c: c["score"])
            heading = circular_mean(
                [(a, wt) for a, wt in brg_w[b]
                 if best["from"] <= a < best["from"] + self.sector]) if best["score"] > 0 else None
            unworked, newmult = {}, {}
            qtc_val = 0.0
            for L in live[b]:
                bc = base_call(L["dx"])
                if (bc, b) in worked_cb or bc in self.mycalls:
                    continue
                w = self.where(L["dx"])
                if not w:
                    continue
                aud, _ = self.audibility(L, w, b, cells, now, night_home)
                if aud > unworked.get(bc, 0):
                    unworked[bc] = aud
                if w["mult"] not in mults[b]:
                    newmult[w["mult"]] = max(aud, newmult.get(w["mult"], 0))
                if not w["eu"]:
                    room = max(0, 10 - self.qtc_for(bc)["total"])
                    qtc_val = qtc_val + aud * self.qtc_value * room
            eff_q, eff_m = sum(unworked.values()), sum(newmult.values())
            value = eff_q + mult_eq[b] * eff_m + qtc_val
            bands_out.append({
                "band": b, "weight": WEIGHT[b], "cells": cells,
                "best_from": best["from"], "best_score": best["score"],
                "heading": round(heading) if heading is not None else None,
                "compass": compass(heading) if heading is not None else "",
                "unworked": len(unworked), "new_mults": len(newmult), "value": round(value, 1),
                "unworked_eff": round(eff_q, 1), "new_mults_eff": round(eff_m, 1),
                "new_mults_here": sum(1 for v in newmult.values() if v >= 0.6),
                "mult_eq": round(mult_eq[b], 1), "qtc_val": round(qtc_val, 1),
                "hear_cont": {k: len(v) for k, v in sorted(cont_hear[b].items(), key=lambda x: -len(x[1]))},
                "path_cont": {k: len(v) for k, v in sorted(cont_path[b].items(), key=lambda x: -len(x[1]))},
                "me_count": sum(len(x) for x in me[b]),
                "activity": len(live[b]), "worked_mults": len(mults[b]),
            })
        ranked = sorted(bands_out, key=lambda x: -x["value"])
        cells_by_band = {x["band"]: x["cells"] for x in bands_out}

        # ---- mult alerts
        best_alert = {}
        for (dxc, band), L in self.latest.items():
            if now - L["last"] > self.alert_age:
                continue
            bc = base_call(dxc)
            if (bc, band) in worked_cb or bc in self.mycalls:
                continue
            w = self.where(dxc)
            if not w or w["mult"] in mults[band]:
                continue
            aud, why = self.audibility(L, w, band, cells_by_band[band], now, night_home)
            snrs = [v[1] for v in L["spotters"].values() if v[1] is not None]
            a = {"dx": dxc, "band": band, "freq": L["freq"], "pfx": w["mult"],
                 "name": w["name"], "cont": w["cont"], "brg": round(w["brg"]),
                 "lp": round((w["brg"] + 180) % 360), "km": round(w["dist"]),
                 "value": WEIGHT[band], "eq": round(mult_eq[band] * aud, 1),
                 "age_s": int(now - L["last"]), "spotters": len(L["spotters"]),
                 "human": L["human"], "snr": max(snrs) if snrs else None, "cq": L["cq"],
                 "sun": sun_state(solar_elevation(w["lat"], w["lon"])), "aud": aud, "why": why}
            k = (w["mult"], band)
            if k not in best_alert or (aud, -a["age_s"]) > (best_alert[k]["aud"], -best_alert[k]["age_s"]):
                best_alert[k] = a
        alerts = sorted(best_alert.values(), key=lambda a: (-a["value"] * a["aud"], a["age_s"]))
        for a in alerts:
            # beep only once per mult/band, only if workable and not a single-skimmer (possible bust)
            reliable = a["spotters"] >= 2 or a["human"]
            k = (a["pfx"], a["band"])
            a["fresh"] = reliable and a["aud"] >= 0.25 and k not in self.alerts_seen
            if a["fresh"]:
                self.alerts_seen.add(k)

        # ---- QTC targets
        stock = max(0, len(self.qsos) - qt["sent"])
        qtc_t = {}
        for (dxc, band), L in self.latest.items():
            if now - L["last"] > self.alert_age:
                continue
            w = self.where(dxc)
            if not w or w["eu"]:
                continue
            bc = base_call(dxc)
            if bc in self.mycalls:
                continue
            q = self.qtc_for(bc)
            room = 10 - q["total"]
            if room <= 0:
                continue
            aud, why = self.audibility(L, w, band, cells_by_band[band], now, night_home)
            row = {"dx": dxc, "band": band, "freq": L["freq"], "cont": w["cont"], "aud": aud, "why": why,
                   "room": room, "sent": q["sent"], "rcvd": q["rcvd"],
                   "worked_band": (bc, band) in worked_cb, "worked_any": bc in worked_call,
                   "brg": round(w["brg"]), "cq": L["cq"], "age_s": int(now - L["last"])}
            if bc not in qtc_t or (aud, -row["age_s"]) > (qtc_t[bc]["aud"], -qtc_t[bc]["age_s"]):
                qtc_t[bc] = row
        qtc_t = sorted(qtc_t.values(), key=lambda x: (-x["aud"], not x["cq"], -x["room"], x["age_s"]))[:40]

        # ---- rivals
        rivals = []
        for r in self.rivals:
            seen = sorted((L for (dxc, band), L in self.latest.items() if base_call(dxc) == r),
                          key=lambda L: -L["last"])
            row = {"call": r}
            if seen:
                L = seen[0]
                conts = []
                for sc, (t, snr) in L["spotters"].items():
                    w = self.where(sc)
                    if w and t > now - self.window:
                        conts.append(w["cont"])
                row.update(band=L["band"], freq=L["freq"], age_s=int(now - L["last"]), cq=L["cq"],
                           heard_by=dict(sorted({c: conts.count(c) for c in set(conts)}.items(),
                                                key=lambda x: -x[1])),
                           bands_seen=sorted({L2["band"] for L2 in seen if now - L2["last"] < 1800}))
            if self.scoreboard:
                sb = self.scoreboard.summary(r)
                if sb:
                    row["sb"] = sb
            if self.hamqth and self.hamqth.cache.get(r):
                row["info"] = self.hamqth.cache[r]
            rivals.append(row)

        rise, sset = sunrise_sunset(*self.home)
        my_heard.sort(key=lambda x: -x["t"])
        seen_sk, my_list = set(), []
        for h in my_heard:
            k = (h["skimmer"], h["band"])
            if k in seen_sk:
                continue
            seen_sk.add(k)
            h["age_s"] = int(now - h.pop("t"))
            my_list.append(h)

        return {
            "utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
            "mycall": self.mycall, "home": [round(self.home[0], 3), round(self.home[1], 3)],
            "sun": {"rise": rise, "set": sset, "state": sun_state(solar_elevation(*self.home))},
            "solar": self.solar, "radio": self.radio, "sector": self.sector,
            "rate": self.my_rate(now), "optime": opt,
            "mults": {str(b): len(mults[b]) for b in CONTEST_BANDS},
            "weighted_mults": wm,
            "score": {"points": points, "qsos": len(worked_cb), "est": points * wm,
                      "p_proj": round(p_proj), "w_proj": round(w_proj)},
            "qtc": {"stock_est": stock, "eu_qsos": eu_q, **qt,
                    "source": "N1MM DB" if self.db_qtc["lines"] else "manual"},
            "bands": bands_out, "ranked": [x["band"] for x in ranked],
            "alerts": alerts[:60], "qtc_targets": qtc_t, "rivals": rivals, "my_heard": my_list[:80],
            "feeds": [vars(f.status) for f in self.feeds],
            "control": self.control.info() if getattr(self, "control", None) else {},
            "status": {**self.status,
                       "scoreboard": self.scoreboard.status if self.scoreboard else "disabled",
                       "hamqth": self.hamqth.status if self.hamqth else "disabled",
                       "prop_spots_window": win_n, "spots_ingested": self.ingested},
        }

    def lookup(self, call):
        call = call.upper().strip()
        w = self.where(call)
        out = {"call": call, "cty": w}
        if w:
            out["sun"] = sun_state(solar_elevation(w["lat"], w["lon"]))
            out["lp"] = round((w["brg"] + 180) % 360)
        out["spots"] = [{"band": L["band"], "freq": L["freq"], "age_s": int(time.time() - L["last"]),
                         "spotters": len(L["spotters"]), "cq": L["cq"]}
                        for (dxc, b), L in self.latest.items() if base_call(dxc) == base_call(call)]
        out["worked"] = sorted({q[1] for q in self.qsos.values() if base_call(q[0]) == base_call(call)})
        out["qtc"] = self.qtc_for(call)
        if self.hamqth:
            self.hamqth.request(base_call(call))
            out["hamqth"] = self.hamqth.cache.get(base_call(call))
        return out
