#!/usr/bin/env python3
"""Demo mode: run the full dashboard on simulated data — no radio, no N1MM, no internet needed.

    python demo/run_demo.py            then open http://localhost:8073

It starts, all on this computer:
  * a fake RBN/cluster telnet server (127.0.0.1:17000) that streams made-up skimmer spots,
  * a fake N1MM database with a few QSOs and QTCs (in a temp folder),
  * a fake Contest Online Scoreboard page (127.0.0.1:17001) with fictional rivals ZZ1AA..ZZ5EE,
  * fake N1MM radio broadcasts,
and points the real program at them. Every call and number you see is simulated.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import random
import socket
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from aiohttp import web  # noqa: E402

from waeintel import app as wae  # noqa: E402
from waeintel import config as config_mod  # noqa: E402

MYCALL, GRID = "ZZ9DEM", "JN76GB"
NEAR = ["DK8NE", "OE9GHV", "9A1CIG", "HA1VHF", "S50ARX"]        # skimmers near JN76
FAR = ["W3LPL", "K1TTT", "W6YX", "JA1XYZ", "VE3EID"]           # far skimmers
DX = [("W1AW", 14085), ("K3LR", 14088), ("W6YI", 14092), ("JA1ZZZ", 7045), ("VE3AAA", 7040),
      ("PY2AA", 21085), ("VK6ABC", 21090), ("RA9AA", 14080), ("EA8AH", 3580), ("K5ZD", 3585)]
EU = ["DL1ABC", "9A5Y", "OE1XYZ", "HA8RT", "IT9BLB", "SP5AA", "EA5AA", "G0AAA", "OZ1AA", "F5AAA"]
TELNET_PORT, SB_PORT, UDP_PORT, WEB_PORT = 17000, 17001, 12061, 8073

SCOREBOARD_HTML = """<html><body><table>
<tr><td>SO-ALL HP DIGI</td><td>Score</td><td>QSO</td><td>Countries</td><td>Last</td><td>Log</td></tr>
<tr><td>1</td><td>ZZ1AA</td><td>48,100</td><td>402</td><td>119</td><td>02:55</td></tr>
<tr><td>2</td><td>ZZ2BB</td><td>31,900</td><td>311</td><td>101</td><td>02:56</td></tr>
<tr><td>3</td><td>ZZ3CC</td><td>{s3}</td><td>260</td><td>92</td><td>02:54</td></tr>
<tr><td>4</td><td>ZZ9DEM</td><td>{me}</td><td>51</td><td>31</td><td>02:59</td></tr>
<tr><td>5</td><td>ZZ4DD</td><td>{s4}</td><td>201</td><td>84</td><td>02:57</td></tr>
<tr><td>6</td><td>ZZ5EE</td><td>15,200</td><td>170</td><td>80</td><td>02:52</td></tr>
</table></body></html>"""


async def telnet_handler(reader, writer):
    writer.write(b"Welcome to the demo feed\r\nPlease enter your call: ")
    await writer.drain()
    await reader.readline()
    try:
        while True:
            k = random.random()
            if k < 0.45:
                sk, (call, f) = random.choice(NEAR), random.choice(DX)
                mode, unit = "RTTY", "BPS"
            elif k < 0.75:
                sk, call, f = random.choice(FAR), random.choice(EU), random.choice([3575, 7042, 14083, 21088, 28085])
                mode, unit = random.choice([("RTTY", "BPS"), ("CW", "WPM")])
            else:
                sk, call, f = random.choice(FAR), MYCALL, random.choice([7044, 14090, 21080])
                mode, unit = "RTTY", "BPS"
            line = (f"DX de {sk}-#:  {f + random.random():8.1f}  {call:<12} {mode}  {random.randint(3, 35):2d} dB"
                    f"  45 {unit}  {'CQ' if random.random() < .6 else 'DX'}      {time.strftime('%H%M', time.gmtime())}Z\r\n")
            writer.write(line.encode())
            await writer.drain()
            await asyncio.sleep(0.05)
    except (ConnectionError, asyncio.CancelledError):
        pass


def make_db(path, start):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE DXLOG (ID INTEGER PRIMARY KEY, Call TEXT, Band REAL, TS TEXT, Mode TEXT,"
                " ContestName TEXT, ContestNR INTEGER)")
    con.execute("CREATE TABLE QTCs (ID INTEGER PRIMARY KEY, ContestNR INTEGER, TS TEXT, Call TEXT, QTCCall TEXT,"
                " QTCNr INTEGER, IsReceived INTEGER)")
    now = dt.datetime.now(dt.timezone.utc)
    ts = lambda m: (now - dt.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    qsos = [("DL1ABC", 14, 170), ("W1AW", 14, 165), ("9A5Y", 7, 90), ("K3LR", 14, 30), ("OE1XYZ", 14, 8),
            ("HA8RT", 7, 6), ("JA1ZZZ", 7, 4), ("VE3AAA", 7, 3), ("EA5AA", 21, 2), ("W6YI", 14, 1)]
    qsos += [(f"DL{i}XY", 7 if i % 2 else 3.5, 150 - i * 3) for i in range(40)]
    for c, b, m in qsos:
        con.execute("INSERT INTO DXLOG(Call,Band,TS,Mode,ContestName,ContestNR) VALUES(?,?,?,?,'WAERTTY',7)",
                    (c, b, ts(m), "RTTY"))
    for i in range(10):
        con.execute("INSERT INTO QTCs(ContestNR,TS,Call,QTCCall,QTCNr,IsReceived) VALUES(7,?,?,?,?,0)",
                    (ts(160), "W1AW", f"DL{i}XY", i + 1))
    for i in range(3):
        con.execute("INSERT INTO QTCs(ContestNR,TS,Call,QTCCall,QTCNr,IsReceived) VALUES(7,?,?,?,?,1)",
                    (ts(3), "JA1ZZZ", f"X{i}", i + 1))
    con.commit()
    con.close()


async def main():
    tmp = tempfile.mkdtemp(prefix="wae_demo_")
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(hours=3)
    db = os.path.join(tmp, "demo.s3db")
    make_db(db, start)
    cfg = config_mod._merge(config_mod.DEFAULTS, {
        "station": {"call": MYCALL, "grid": GRID},
        "contest": {"start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_utc": (start + dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        "files": {"cty": os.path.join(HERE, "cty_sample.dat"), "qtc_ledger": os.path.join(tmp, "qtc.json"),
                  "skimmers_csv": ""},
        "web": {"host": "127.0.0.1", "port": WEB_PORT},
        "n1mm": {"udp_port": UDP_PORT, "db_path": db, "db_poll_seconds": 5},
        "control": {"n1mm_port": 13065},            # demo: don't talk to a real N1MM on 13064
        "scoreboard": {"url": f"http://127.0.0.1:{SB_PORT}/", "poll_seconds": 15, "category": "SO HP"},
        "solar": {"url": ""},
    })
    cfg["feeds"] = [{"name": "DEMO-RBN", "host": "127.0.0.1", "port": TELNET_PORT, "enabled": True}]
    config_mod.resolve_dates(cfg["contest"])
    config_mod.validate(cfg)

    await asyncio.start_server(telnet_handler, "127.0.0.1", TELNET_PORT)
    t0 = time.time()

    async def board(_):
        grow = (time.time() - t0) / 60          # scores creep up so rates appear after ~10 min
        return web.Response(text=SCOREBOARD_HTML.format(s3=f"{24300 + int(grow * 25):,}",
                                                        me=f"{22704 + int(grow * 35):,}",
                                                        s4=f"{19800 + int(grow * 15):,}"), content_type="text/html")
    sb = web.Application()
    sb.router.add_get("/", board)
    runner = web.AppRunner(sb)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", SB_PORT).start()

    async def radio():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while True:
            s.sendto(b'<?xml version="1.0"?><RadioInfo><RadioNr>1</RadioNr><Freq>1408830</Freq><Mode>RTTY</Mode>'
                     b'<ActiveRadioNr>1</ActiveRadioNr><IsTransmitting>False</IsTransmitting></RadioInfo>',
                     ("127.0.0.1", UDP_PORT))
            await asyncio.sleep(10)
    asyncio.ensure_future(radio())
    print(f"\n  DEMO running with simulated data -> open http://localhost:{WEB_PORT}   (Ctrl+C to stop)\n")
    await wae.main(cfg)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
