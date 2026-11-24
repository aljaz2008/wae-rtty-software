"""WAE RTTY Intel — live RBN / DX-cluster / scoreboard dashboard for the WAEDC RTTY contest.

Run:  python wae_intel.py            (uses ./config.toml)
Open: http://localhost:8073
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import argparse

import aiohttp
from aiohttp import web

from . import __version__
from . import config as config_mod
from .analytics import State
from .cty import Cty
from .external import HamQTH, Scoreboard, solar_poller
from .n1mm import N1MMDB, db_poller, find_db, inspect_db, start_udp
from .control import Control
from .spots import TelnetFeed

HERE = Path(__file__).resolve().parent
log = logging.getLogger("wae")


def find_cty(path):
    """Configured cty.dat, else N1MM's own country file if present."""
    if os.path.exists(path):
        return path
    alt = os.path.join(os.path.expanduser("~"), "Documents", "N1MM Logger+", "SupportFiles", "wl_cty.dat")
    if os.path.exists(alt):
        logging.warning("%s not found, using N1MM's %s", path, alt)
        return alt
    return None


def build_app(state: State):
    app = web.Application()
    clients: set = set()

    async def index(_):
        return web.FileResponse(HERE / "static" / "index.html")

    async def snap(_):
        state.perf["http_snapshots"] += 1
        return web.json_response(state.snapshot())

    async def lookup(req):
        return web.json_response(state.lookup(req.query.get("call", "")))

    async def qtc(req):
        d = await req.json()
        if not d.get("call"):
            return web.json_response({"error": "call required"}, status=400)
        s = state.qtc_update(d["call"], int(d.get("sent", 0)), int(d.get("rcvd", 0)))
        return web.json_response({"call": d["call"].upper(), **s})

    async def qsy(req):
        d = await req.json()
        ctl = state.control
        out = {}
        if d.get("freq"):
            ok, msg = ctl.qsy(float(d["freq"]))
            out["qsy"] = {"ok": ok, "msg": msg}
        if d.get("rotate") and d.get("azimuth") is not None:
            ok, msg = ctl.rotate(float(d["azimuth"]), d.get("band"))
            out["rotor"] = {"ok": ok, "msg": msg}
        return web.json_response(out)

    async def ws(req):
        w = web.WebSocketResponse(heartbeat=30)
        await w.prepare(req)
        clients.add(w)
        state.perf["ws_clients"] = len(clients)
        try:
            await w.send_str(json.dumps(state.snapshot()))
            async for _ in w:
                pass
        finally:
            clients.discard(w)
            state.perf["ws_clients"] = len(clients)
        return w

    async def pusher(app_):
        async def loop():
            every = state.cfg["web"].get("push_seconds", 5)
            while True:
                await asyncio.sleep(every)
                try:
                    data = json.dumps(state.snapshot())
                except Exception:  # noqa: BLE001
                    log.exception("snapshot failed")
                    continue
                for c in list(clients):
                    try:
                        await c.send_str(data)
                    except Exception:  # noqa: BLE001
                        clients.discard(c)
        t = asyncio.create_task(loop())
        yield
        t.cancel()

    app.router.add_get("/", index)
    app.router.add_get("/api/snapshot", snap)
    app.router.add_get("/api/lookup", lookup)
    app.router.add_post("/api/qtc", qtc)
    app.router.add_post("/api/qsy", qsy)
    app.router.add_get("/ws", ws)
    app.cleanup_ctx.append(pusher)
    return app


async def main(cfg):
    cty_path = find_cty(cfg["files"]["cty"])
    if not cty_path:
        sys.exit(f"cty file not found: {cfg['files']['cty']}\n"
                 f"  download cty.dat (Big CTY) from https://www.country-files.com and put it next to config.toml")
    cty = Cty(cty_path)
    log.info("cty: %d entities (%d WAE-only)", len(cty.entities),
             sum(e.wae_only for e in cty.entities))
    state = State(cfg, cty)
    state.control = Control(cfg, state)

    tasks = []
    for fc in cfg.get("feeds", []):
        if not fc.get("enabled", True):
            continue
        feed = TelnetFeed(fc["name"], fc["host"], fc["port"],
                          fc.get("login") or cfg["station"].get("login_call") or cfg["station"]["call"],
                          fc.get("password"), fc.get("init", []), state.on_line,
                          stale_s=fc.get("stale_seconds", 600))
        state.feeds.append(feed)
        tasks.append(asyncio.create_task(feed.run()))

    n = cfg.get("n1mm", {})
    if n.get("udp_enabled", True):
        try:
            proto = await start_udp(state, n.get("udp_host", "0.0.0.0"), n.get("udp_port", 12060))
            state.status["n1mm_udp"] = f"listening :{n.get('udp_port', 12060)}"
            state._n1mm_proto = proto
        except OSError as e:
            state.status["n1mm_udp"] = f"error: {e}"
    db_path = n.get("db_path") or ""
    if db_path and (db_path.lower() != "auto" or find_db("auto")):
        db = N1MMDB(db_path, state.start, state.end, n.get("contest_like", "WAE%"), n.get("qtc", {}),
                    copy_mode=n.get("db_mode", "direct") == "copy")
        tasks.append(asyncio.create_task(db_poller(state, db, n.get("db_poll_seconds", 10))))
    else:
        state.status["n1mm_db"] = "off (no N1MM database found; log comes from UDP broadcasts only)"

    session = aiohttp.ClientSession()
    sb_cfg = cfg.get("scoreboard", {})
    state.scoreboard = Scoreboard(sb_cfg.get("url", ""), sb_cfg.get("poll_seconds", 60),
                                  cfg.get("rivals", {}).get("calls", []))
    tasks.append(asyncio.create_task(state.scoreboard.run(session)))
    if cfg.get("solar", {}).get("url"):
        tasks.append(asyncio.create_task(solar_poller(session, state, cfg["solar"]["url"])))
    hq = cfg.get("hamqth", {})
    state.hamqth = HamQTH(hq.get("user", ""), hq.get("password", ""))
    tasks.append(asyncio.create_task(state.hamqth.run(session, state.set_location)))

    async def lag_probe():
        loop = asyncio.get_running_loop()
        while True:
            t = loop.time()
            await asyncio.sleep(1.0)
            lag = max(0.0, (loop.time() - t - 1.0) * 1000)   # Windows timers can wake a bit early
            state.perf["lag_ms"] = (state.perf["lag_ms"] + [lag])[-60:]
    tasks.append(asyncio.create_task(lag_probe()))

    app = build_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    host, port = cfg["web"].get("host", "127.0.0.1"), cfg["web"].get("port", 8073)
    await web.TCPSite(runner, host, port).start()
    c = cfg["contest"]
    log.info("WAE RTTY Intel %s for %s (%s) — contest window %s .. %s%s", __version__, cfg["station"]["call"],
             cfg["station"]["grid"], c["start_utc"], c["end_utc"], " (auto)" if c.get("dates_auto") else "")
    log.info("dashboard: http://localhost:%s%s", port,
             "  (also on your LAN)" if host in ("0.0.0.0", "::") else "")
    def _report(t):
        if not t.cancelled() and t.exception():
            log.error("background task crashed: %r", t.exception())
    for t in tasks:
        t.add_done_callback(_report)
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.Event().wait()     # keep the dashboard up even if every task ended
    finally:
        await session.close()
        await runner.cleanup()


def check(cfg, path):
    """Print what the program will use, then exit (python wae_intel.py --check)."""
    st, c, n = cfg["station"], cfg["contest"], cfg["n1mm"]
    print(f"config        : {os.path.abspath(path)}")
    print(f"station       : {st['call']} @ {st['grid']}  aliases={st['aliases'] or '-'}")
    print(f"contest window: {c['start_utc']} .. {c['end_utc']}{'  (auto: 2nd full weekend of November)' if c.get('dates_auto') else ''}")
    cty = find_cty(cfg["files"]["cty"])
    print(f"cty file      : {cty or 'NOT FOUND - download cty.dat from country-files.com'}")
    db = n.get("db_path") or ""
    print(f"N1MM database : {(find_db(db) or 'not found (optional)') if db else 'disabled'}")
    print(f"N1MM UDP      : {'listen ' + n['udp_host'] + ':' + str(n['udp_port']) if n['udp_enabled'] else 'disabled'}")
    print(f"web           : http://{cfg['web']['host']}:{cfg['web']['port']}")
    print(f"scoreboard    : {cfg['scoreboard']['url'] or 'disabled'}  category={cfg['scoreboard']['category'] or 'all'}")
    print("feeds         : " + ", ".join(f"{f['name']}({f['host']}:{f['port']})" for f in cfg["feeds"] if f.get("enabled", True)))
    print(f"click-to-QSY  : {'on -> ' + cfg['control']['n1mm_host'] + ':' + str(cfg['control']['n1mm_port']) if cfg['control']['qsy_on_click'] else 'off'}"
          f"   rotator: {cfg['control']['rotator']}")
    return 0 if cty else 1


def cli(argv=None):
    ap = argparse.ArgumentParser(prog="wae_intel", description="Live band/beam/mult assistant for the WAE DX Contest RTTY.")
    ap.add_argument("config", nargs="?", default="config.toml", help="path to config.toml (default: ./config.toml)")
    ap.add_argument("--check", action="store_true", help="validate the config, show what will be used, and exit")
    ap.add_argument("--inspect-db", action="store_true", help="print the N1MM database tables and detected QSO/QTC mapping")
    ap.add_argument("--version", action="version", version=f"WAE RTTY Intel {__version__}")
    a = ap.parse_args(argv)
    try:
        cfg = config_mod.load(a.config, require_station=not a.inspect_db)
    except config_mod.ConfigError as e:
        sys.exit(f"error: {e}")
    os.chdir(Path(a.config).resolve().parent)
    logging.basicConfig(level=cfg.get("log_level", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.check:
        sys.exit(check(cfg, a.config))
    if a.inspect_db:
        n = cfg["n1mm"]
        # all dates, so test QSOs/QTCs logged before the contest start are shown too
        inspect_db(n.get("db_path") or "auto", 0, 4e9, n.get("contest_like", "WAE%"), n.get("qtc", {}))
        return
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
