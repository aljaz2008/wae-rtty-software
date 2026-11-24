"""Unit tests: python -m pytest -q"""
import os
import sys
import time
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from waeintel import config as C  # noqa: E402
from waeintel.analytics import State, area_digit, base_call  # noqa: E402
from waeintel.cty import Cty  # noqa: E402
from waeintel.external import parse_scoreboard  # noqa: E402
from waeintel.geo import bearing_distance, grid_to_latlon  # noqa: E402
from waeintel.spots import freq_to_band, parse_spot  # noqa: E402

CTY = os.path.join(ROOT, "demo", "cty_sample.dat")
SEG = [(14060, 14098.9), (14101.1, 14150), (7025, 7100), (3560, 3600)]


@pytest.fixture(scope="module")
def cty():
    return Cty(CTY)


def make_state(cty, tmp_path, **over):
    cfg = C._merge(C.DEFAULTS, {"station": {"call": "S51A", "grid": "JN76GB", "aliases": ["S58Q"]},
                                "files": {"qtc_ledger": str(tmp_path / "q.json"), "skimmers_csv": ""}, **over})
    C.resolve_dates(cfg["contest"])
    return State(cfg, cty)


def test_grid():
    lat, lon = grid_to_latlon("JN76GB")
    assert 46.0 < lat < 46.1 and 14.5 < lon < 14.6
    with pytest.raises(ValueError):
        grid_to_latlon("ZZ99")


def test_bearing_to_usa_is_northwest():
    brg, dist = bearing_distance(46.06, 14.54, 42.5, -71.8)
    assert 290 < brg < 310 and 6000 < dist < 7000


def test_wae_dates():
    s, e = C.wae_rtty_window(2026)
    assert s == datetime(2026, 11, 14, tzinfo=timezone.utc)
    assert e.strftime("%Y-%m-%d %H:%M:%S") == "2026-11-15 23:59:59"
    s, _ = C.wae_rtty_window(2025)
    assert s.day == 8          # Nov 2025: first full weekend 1-2, second 8-9


def test_cty_lookup(cty):
    assert cty.lookup("S58Q").entity.prefix == "S5"
    assert cty.lookup("EA8/DL1ABC").entity.prefix == "EA8"
    assert cty.lookup("DL1ABC/P").entity.prefix == "DL"
    it9 = cty.lookup("IT9BLB")
    assert it9.entity.prefix == "IT9" and it9.entity.wae_only
    assert cty.lookup("W1AW/MM") is None


def test_call_area_mults(cty, tmp_path):
    st = make_state(cty, tmp_path)
    assert st.where("W1AW")["mult"] == "W1"
    assert st.where("K6XX")["mult"] == "W6"
    assert st.where("JA1ZZZ")["mult"] == "JA1"
    assert st.where("RA9AA")["mult"] == "RA9"
    assert st.where("IT9BLB")["mult"] == "*IT9"
    assert area_digit("7M4ABC") == "4"
    assert base_call("EA8/DL1ABC") == "DL1ABC"


def test_parse_rbn_and_cluster_spots():
    s = parse_spot("DX de DK8NE-#:    14085.0  W1AW         RTTY  22 dB  45 BPS  CQ      2112Z", "RBN", SEG)
    assert (s.spotter, s.dx, s.band, s.mode, s.snr, s.is_cq, s.skimmer) == ("DK8NE", "W1AW", 20, "RTTY", 22, True, True)
    s = parse_spot("DX de S50ABC:     7045.0  JA1XYZ       tnx qso              2201Z", "X", SEG)
    assert s.mode == "RTTY?" and s.band == 40
    s = parse_spot("DX de S50ABC:     14074.5  JA1XYZ       tnx              2201Z", "X", SEG)
    assert s.mode == "FT?"         # unlabeled spot on an FT8 frequency is not a RTTY target
    assert freq_to_band(3580) == 80 and freq_to_band(14099) == 20


def test_scoreboard_html_with_operator_call():
    html = """<table><tr><td>SO-ALL HP DIGI</td><td>Score</td><td>QSO</td><td>Countries</td><td>Last</td></tr>
    <tr><td>39</td><td><a href="/x?call=S51A">S51A (S58Q)</a></td><td>6,726</td><td>118</td><td>57</td><td>00:00</td></tr>
    <tr><td>40</td><td>VE7BC</td><td>5,940</td><td>90</td><td>66</td><td>00:00</td></tr></table>"""
    rows = parse_scoreboard(html)
    assert rows[0] == {"call": "S51A", "op": "S58Q", "score": 6726.0, "qsos": 118.0, "mults": 57.0,
                       "category": "SO-ALL HP DIGI", "last": "00:00"}
    assert rows[1]["call"] == "VE7BC"


def test_category_match(cty, tmp_path):
    st = make_state(cty, tmp_path, scoreboard={"category": "SO HP"})
    ok = lambda c: st.cat_match({"category": c})  # noqa: E731
    assert ok("SO-ALL HP DIGI") and ok("S/O HP") and not ok("S/O LP") and not ok("M/S HP") and not ok("S/O 20m HP")


def test_off_time(cty, tmp_path):
    st = make_state(cty, tmp_path, contest={"start_utc": "2026-11-14T00:00:00Z", "end_utc": "2026-11-15T23:59:59Z"})
    t0 = st.start
    for i, m in enumerate([5, 30, 50, 140, 150]):          # gap 50 -> 140 min = 90 min off
        st.upsert_qso(f"q{i}", f"DL{i}AA", 20, t0 + m * 60, "RTTY")
    o = st.op_time(t0 + 160 * 60)
    assert len(o["offs"]) == 1 and abs(o["off_total_h"] - 1.5) < 0.01
    assert abs(o["used_h"] - (160 - 90) / 60) < 0.01


def test_db_guard_keeps_udp_log(cty, tmp_path):
    st = make_state(cty, tmp_path, contest={"start_utc": "2020-01-01T00:00:00Z", "end_utc": "2099-01-01T00:00:00Z"})
    st.upsert_qso("u1", "W1AW", 20, time.time() - 600, "RTTY")
    st.sync_db([], None)                                       # wrong DB returns nothing
    assert len(st.qsos) == 1 and "n1mm_db_warn" in st.status
