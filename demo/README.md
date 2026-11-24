# Demo mode

```
python demo/run_demo.py
```

Opens the full dashboard at http://localhost:8073 using **simulated** data only:
a fake RBN/cluster telnet feed, a fake N1MM database with a few QSOs and QTCs, a fake
Contest Online Scoreboard page and fake N1MM radio broadcasts — all on 127.0.0.1.
No radio, N1MM or internet connection is needed. Every call and number shown is made up
(the demo station is `ZZ9DEM`, rivals are `ZZ1AA`–`ZZ5EE`).

`cty_sample.dat` is a tiny hand-made country file for the demo and the tests. For real use,
download the Big CTY `cty.dat` from https://www.country-files.com.
