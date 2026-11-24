"""Configuration: load TOML, apply defaults, compute WAE RTTY dates, validate."""
from __future__ import annotations

import copy
import os
import re
from datetime import datetime, timedelta, timezone

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

from .geo import grid_to_latlon

CALL_RE = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}[A-Z](/[A-Z0-9]+)?$")

DEFAULT_FEEDS = [
    {"name": "RBN", "host": "telnet.reversebeacon.net", "port": 7000, "enabled": True},
    {"name": "S50CLX", "host": "s50clx.s50e.si", "port": 41112, "enabled": True},
    {"name": "S50DXS", "host": "s50dxs.s53m.com", "port": 8000, "enabled": True},
    {"name": "GB7DXG", "host": "gb7dxg.lebs.org.uk", "port": 7373, "enabled": True},
    {"name": "VE7CC", "host": "ve7cc.net", "port": 23, "enabled": True},
    {"name": "W3LPL", "host": "dxc.w3lpl.net", "port": 7373, "enabled": True},
]

DEFAULTS = {
    "log_level": "INFO",
    "station": {"call": "", "grid": "", "aliases": []},
    "contest": {"start_utc": "auto", "end_utc": "auto", "max_operating_hours": 36, "min_off_minutes": 60},
    "analysis": {
        "window_min": 15, "sector_deg": 30, "near_km": 1200, "far_km": 2500, "local_km": 400,
        "alert_max_age_min": 10, "expected_final_points": 2500, "expected_final_wmults": 800,
        "qtc_value": 0.3, "mult_decay_hours": 8.0, "rtty_only": True, "propagation_all_modes": True,
        "rtty_segments": [[3560, 3600], [7025, 7100], [14060, 14098.9], [14101.1, 14150],
                          [21060, 21150], [28060, 28200]],
    },
    "files": {"cty": "cty.dat", "qtc_ledger": "qtc_ledger.json", "skimmers_csv": "skimmers.csv"},
    "web": {"host": "127.0.0.1", "port": 8073, "push_seconds": 5},
    "n1mm": {"udp_enabled": True, "udp_host": "0.0.0.0", "udp_port": 12060, "db_path": "auto",
             "db_poll_seconds": 10, "db_mode": "direct", "contest_like": "WAE%", "qtc": {}},
    "control": {"qsy_on_click": True, "n1mm_host": "127.0.0.1", "n1mm_port": 13064, "radio_nr": "active",
                "qsy_offset_khz": 0.0, "block_while_transmitting": True, "rotator": "none",
                "rotator_host": "127.0.0.1", "rotator_name": "", "rotate_on_click": False},
    "scoreboard": {"url": "https://contestonlinescore.com/scoreboard/?contest_id=101", "poll_seconds": 60,
                   "category": "", "rival_duty_cycle": 0.75},
    "rivals": {"calls": [], "auto": True, "auto_top": 5, "auto_around": 3},
    "solar": {"url": "https://www.hamqsl.com/solarxml.php"},
    "hamqth": {"user": "", "password": "", "lookup_skimmers": True},
}


class ConfigError(Exception):
    pass


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def wae_rtty_window(year: int):
    """WAEDC RTTY: second full weekend of November, Sat 00:00 UTC - Sun 23:59:59 UTC."""
    d = datetime(year, 11, 1, tzinfo=timezone.utc)
    while d.weekday() != 5:          # first Saturday (its Sunday is always in November too)
        d += timedelta(days=1)
    start = d + timedelta(days=7)
    return start, start + timedelta(days=2) - timedelta(seconds=1)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_dates(contest, now=None):
    now = now or datetime.now(timezone.utc)
    if str(contest.get("start_utc", "auto")).lower() == "auto" or str(contest.get("end_utc", "auto")).lower() == "auto":
        start, end = wae_rtty_window(now.year)
        if now > end:
            start, end = wae_rtty_window(now.year + 1)
        contest["start_utc"], contest["end_utc"] = _iso(start), _iso(end)
        contest["dates_auto"] = True
    for k in ("start_utc", "end_utc"):
        try:
            datetime.fromisoformat(contest[k].replace("Z", "+00:00"))
        except ValueError as e:
            raise ConfigError(f"[contest] {k} = {contest[k]!r} is not an ISO time like 2026-11-14T00:00:00Z") from e
    return contest


def load(path: str, require_station=True):
    if not os.path.exists(path):
        raise ConfigError(f"config file not found: {path}\n"
                          f"  copy config.example.toml to config.toml and set your call and grid")
    with open(path, "rb") as f:
        try:
            user = tomllib.load(f)
        except Exception as e:  # noqa: BLE001
            raise ConfigError(f"{path}: TOML syntax error: {e}") from e
    cfg = _merge(DEFAULTS, user)
    if "feeds" not in user:
        cfg["feeds"] = copy.deepcopy(DEFAULT_FEEDS)
    cfg["station"]["call"] = str(cfg["station"].get("call", "")).upper().strip()
    cfg["station"]["aliases"] = [str(a).upper().strip() for a in cfg["station"].get("aliases", [])]
    resolve_dates(cfg["contest"])
    if require_station:
        validate(cfg)
    return cfg


def validate(cfg):
    errs = []
    call = cfg["station"]["call"]
    if not call or call in ("N0CALL", "MYCALL"):
        errs.append("[station] call is not set")
    elif not CALL_RE.match(call):
        errs.append(f"[station] call {call!r} does not look like a callsign")
    try:
        grid_to_latlon(cfg["station"]["grid"])
    except ValueError:
        errs.append(f"[station] grid {cfg['station']['grid']!r} is not a Maidenhead locator (e.g. JN76GB)")
    sec = cfg["analysis"]["sector_deg"]
    if 360 % sec:
        errs.append(f"[analysis] sector_deg = {sec}: 360 must be divisible by it (15, 20, 30, 45 ...)")
    if cfg["control"]["rotator"] not in ("none", "pstrotator", "n1mmrotor"):
        errs.append("[control] rotator must be \"none\", \"pstrotator\" or \"n1mmrotor\"")
    for i, f in enumerate(cfg.get("feeds", [])):
        if not f.get("host") or not f.get("port"):
            errs.append(f"[[feeds]] #{i + 1} needs host and port")
    if errs:
        raise ConfigError("configuration problems:\n  - " + "\n  - ".join(errs))
    return cfg
