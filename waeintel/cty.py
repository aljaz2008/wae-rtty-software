"""cty.dat parser (country-files.com format) with WAE entities.

cty.dat from country-files.com includes WAE-only entities marked with '*'
(e.g. *TA1, *4U1V, *GM/s, *IT9, *JW/b). For WAEDC these count as separate
entities/multipliers, so they are kept.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .geo import CALL_AREA


@dataclass(frozen=True)
class Entity:
    name: str
    cq: int
    itu: int
    cont: str
    lat: float
    lon: float
    prefix: str          # primary prefix, '*' stripped
    wae_only: bool


@dataclass(frozen=True)
class CallInfo:
    call: str
    entity: Entity
    cq: int
    cont: str
    lat: float
    lon: float

    @property
    def is_eu(self):
        return self.cont == "EU"


_OVR = re.compile(r"\((\d+)\)|\[(\d+)\]|<([-\d.]+)/([-\d.]+)>|\{(\w\w)\}|~([-\d.]+)~")
SUFFIXES = {"P", "M", "QRP", "QRPP", "A", "B", "LH", "J", "R", "T", "X", "N", "QRO"}
NO_ENTITY = {"MM", "AM", "BCN"}


class Cty:
    def __init__(self, path: str):
        self.prefixes: dict[str, tuple] = {}
        self.exact: dict[str, tuple] = {}
        self.entities: list[Entity] = []
        self._load(path)

    def _load(self, path):
        with open(path, encoding="latin-1") as f:
            text = f.read()
        # records end with ';'
        for rec in text.split(";"):
            rec = rec.strip()
            if not rec:
                continue
            lines = rec.split("\n")
            head = lines[0]
            parts = [p.strip() for p in head.split(":")]
            if len(parts) < 8:
                continue
            name, cq, itu, cont, lat, lon, _tz, pfx = parts[:8]
            wae = pfx.startswith("*")
            ent = Entity(name, int(cq), int(itu), cont, float(lat), -float(lon),
                         pfx.lstrip("*"), wae)
            self.entities.append(ent)
            aliases = " ".join(l.strip() for l in lines[1:])
            for tok in aliases.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                exact = tok.startswith("=")
                if exact:
                    tok = tok[1:]
                base = re.split(r"[(\[<{~]", tok, maxsplit=1)[0].strip().upper()
                ovr = {}
                for m in _OVR.finditer(tok):
                    if m.group(1): ovr["cq"] = int(m.group(1))
                    if m.group(3): ovr["lat"], ovr["lon"] = float(m.group(3)), -float(m.group(4))
                    if m.group(5): ovr["cont"] = m.group(5)
                (self.exact if exact else self.prefixes)[base] = (ent, ovr)

    @staticmethod
    def split_call(call: str):
        """Return (lookup_string, base_call, area_digit_override) or None for /MM."""
        call = call.upper().strip()
        parts = [p for p in call.split("/") if p]
        if not parts:
            return None
        if any(p in NO_ENTITY for p in parts[1:]):
            return None
        area = None
        keep = []
        for i, p in enumerate(parts):
            if i > 0 and p in SUFFIXES:
                continue
            if i > 0 and len(p) == 1 and p.isdigit():
                area = p
                continue
            keep.append(p)
        if not keep:
            return None
        if len(keep) == 1:
            return keep[0], keep[0], area
        # prefix/call or call/prefix: shorter part is the prefix
        a, b = keep[0], keep[1]
        pfx, base = (a, b) if len(a) <= len(b) else (b, a)
        return pfx, base, area

    @lru_cache(maxsize=200000)
    def lookup(self, call: str):
        if not call:
            return None
        call = call.upper().strip()
        hit = self.exact.get(call)
        sp = self.split_call(call)
        if sp is None:
            return None
        lk, base, area = sp
        if hit is None:
            hit = self.exact.get(base) if lk == base else None
        if hit is None:
            for n in range(len(lk), 0, -1):
                hit = self.prefixes.get(lk[:n])
                if hit:
                    break
        if hit is None:
            return None
        ent, ovr = hit
        lat, lon = ovr.get("lat", ent.lat), ovr.get("lon", ent.lon)
        # call-area refinement for big countries
        key = {"K": "K", "VE": "VE", "VK": "VK", "PY": "PY"}.get(ent.prefix)
        if key and "lat" not in ovr:
            digit = area
            if digit is None:
                m = re.search(r"\d", lk if lk != base else base)
                digit = m.group(0) if m else None
            if digit and digit in CALL_AREA[key]:
                lat, lon = CALL_AREA[key][digit]
        return CallInfo(call, ent, ovr.get("cq", ent.cq), ovr.get("cont", ent.cont), lat, lon)
