#!/usr/bin/env python3
"""NEPSE opening-range-breakout (ORB) scanner.

Standard library only. NEPSE publishes no intraday bar history, so this tool
RECORDS live snapshots itself (one per minute), builds 5-minute bars from them,
and scans those bars for an opening-range breakout.

  python3 nepse_orb.py record  --source http://localhost:8080/api/v1/market/live
  python3 nepse_orb.py scan    [--at 11:30] [--or-minutes 15] [--day YYYY-MM-DD]
  python3 nepse_orb.py demo    # synthetic session, no network, proves the logic

Times are Nepal time (UTC+05:45, no DST). A scan is a list of charts that
matched a rule, not a list of things to buy. Not financial advice.
"""
import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
import urllib.request

NPT = dt.timezone(dt.timedelta(hours=5, minutes=45))
BAR_MIN = 5

# Defaults. Verify against the current NEPSE notice and edit config.json.
DEFAULTS = {
    "open": "11:00",
    "close": "15:00",
    "trading_days": [0, 1, 2, 3, 4],   # Mon=0 .. Sun=6 (NEPSE moved to Mon-Fri in 2026)
    "circuit_pct": 15.0,               # daily price band, percent
    "circuit_guard_pct": 1.5,          # skip if within this many pct of the band edge
    "max_late_start_min": 30,          # a stock whose first trade is later than this after the open is skipped
    "min_price": 0.0,
    "min_turnover": 500000.0,          # NPR traded so far
    "min_breakout_pct": 0.3,           # close must clear the range high by this much (noise filter)
    "vol_mult": 1.5,                   # breakout bar volume vs average range-bar volume (0 = off)
    "min_range_pct": 0.3,              # opening range must be at least this wide
    "max_range_pct": 8.0,              # ...and at most this wide
    "store": "snapshots.jsonl",
}

# Field names differ between unofficial NEPSE clients; first match wins.
FIELDS = {
    "symbol": ["symbol", "securitySymbol", "s", "ticker"],
    "ltp": ["lastTradedPrice", "lastUpdatedPrice", "ltp", "lastPrice", "closePrice", "close", "price"],
    "volume": ["totalTradeQuantity", "totalTradedQuantity", "volume", "shareTraded", "qty"],
    "turnover": ["totalTradeValue", "totalTradedValue", "turnover", "amount"],
    "prev_close": ["previousClose", "previousDayClosePrice", "prevClose", "prev_close"],
    "high": ["highPrice", "high", "dayHigh"],
    "low": ["lowPrice", "low", "dayLow"],
}


# ------------------------------------------------------------------ helpers

def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


def hhmm(text):
    h, m = text.split(":")
    return dt.time(int(h), int(m))


def pick(row, key):
    for name in FIELDS[key]:
        if name in row and row[name] is not None:
            return row[name]
    return None


def num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def normalise(rows):
    """Raw API rows -> [{symbol, ltp, volume, turnover, prev_close, high, low}]."""
    if isinstance(rows, dict):
        for k in ("data", "result", "content", "liveMarket"):
            if isinstance(rows.get(k), list):
                rows = rows[k]
                break
    out = []
    for r in rows if isinstance(rows, list) else []:
        sym = pick(r, "symbol")
        ltp = num(pick(r, "ltp"))
        if not sym or ltp is None or ltp <= 0:
            continue
        out.append({"symbol": str(sym).strip().upper(), "ltp": ltp,
                    "volume": num(pick(r, "volume")) or 0.0,
                    "turnover": num(pick(r, "turnover")) or 0.0,
                    "prev_close": num(pick(r, "prev_close")),
                    "high": num(pick(r, "high")), "low": num(pick(r, "low"))})
    return out


def fetch_http(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "nepse-orb/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_source(source):
    """source = an http(s) URL returning JSON, or 'py:nepse_data_api' to use that
    library directly (pip install nepse-data-api; no server needed)."""
    if source.startswith("py:"):
        mod = source[3:]
        if mod != "nepse_data_api":
            raise ValueError("only py:nepse_data_api is supported")
        try:
            from nepse_data_api import Nepse  # imported lazily so the demo needs no install
        except ImportError:
            raise SystemExit("Library missing. Run:  pip install nepse-data-api   then try again.")
        return Nepse(enable_cache=False).get_stocks(use_cache=False)
    return fetch_http(source)


def now_npt():
    return dt.datetime.now(NPT).replace(tzinfo=None)


# ------------------------------------------------------------------ store

def append_snapshot(path, when, rows):
    with open(path, "a") as f:
        f.write(json.dumps({"t": when.strftime("%Y-%m-%d %H:%M:%S"), "rows": rows}) + "\n")


def read_snapshots(path, day):
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            t = dt.datetime.strptime(rec["t"], "%Y-%m-%d %H:%M:%S")
            if t.date() == day:
                out.append((t, rec["rows"]))
    out.sort(key=lambda x: x[0])
    return out


# ------------------------------------------------------------------ bars

def build_bars(snaps, day, open_t, close_t, cutoff):
    """{symbol: [bar,...]} 5-minute bars on the grid from open_t, complete at cutoff.
    Bar high/low come from the LTP samples inside the bar (snapshots are ~1/min, so
    this is a coarse high/low, not tick data). Volume/turnover are cumulative -> diffs."""
    start = dt.datetime.combine(day, open_t)
    end = dt.datetime.combine(day, close_t)
    per = {}
    for t, rows in snaps:
        if t < start or t >= end:
            continue
        k = int((t - start).total_seconds() // (BAR_MIN * 60))
        for r in rows:
            per.setdefault(r["symbol"], {}).setdefault(k, []).append((t, r))
    bars = {}
    for sym, by_k in per.items():
        out = []
        for k in sorted(by_k):
            bar_start = start + dt.timedelta(minutes=BAR_MIN * k)
            if bar_start + dt.timedelta(minutes=BAR_MIN) > cutoff:
                continue
            samples = sorted(by_k[k], key=lambda x: x[0])
            ltps = [s[1]["ltp"] for s in samples]
            last = samples[-1][1]
            out.append({"k": k, "start": bar_start, "c": ltps[-1], "h": max(ltps), "l": min(ltps),
                        "vol": last.get("volume", 0.0), "turn": last.get("turnover", 0.0),
                        "prev_close": last.get("prev_close"), "n": len(samples)})
        bars[sym] = out
    return bars


def contiguous(bars, n_range, cutoff, day, open_t):
    """Every 5-min slot from the open to the cutoff must have a bar (no gaps)."""
    want = int((cutoff - dt.datetime.combine(day, open_t)).total_seconds() // (BAR_MIN * 60))
    return want >= n_range and len(bars) == want and all(b["k"] == i for i, b in enumerate(bars))


# ------------------------------------------------------------------ the scan

def orb_scan(bars_by_sym, n_range, cfg):
    hits, counts = [], {"symbols": len(bars_by_sym), "unusable": 0, "illiquid": 0,
                        "narrow_or_wide": 0, "near_circuit": 0, "scanned": 0,
                        "too_late": 0, "late_started": 0}
    scanned = []  # (bars by k, last close) of every stock that passed the filters: the baseline
    for sym, bars in sorted(bars_by_sym.items()):
        # The feed only lists stocks once they have traded, so a stock's opening range
        # starts at its FIRST bar (k0), not necessarily at the open. Bars must be gap-free from k0.
        k0 = bars[0]["k"] if bars else 0
        if len(bars) <= n_range or any(b["k"] != k0 + i for i, b in enumerate(bars)):
            counts["unusable"] += 1
            continue
        if k0 * BAR_MIN > cfg["max_late_start_min"]:
            counts["too_late"] += 1
            continue
        last = bars[-1]
        if last["c"] < cfg["min_price"] or last["turn"] < cfg["min_turnover"]:
            counts["illiquid"] += 1
            continue
        rng = bars[:n_range]
        oh, ol = max(b["h"] for b in rng), min(b["l"] for b in rng)
        width = (oh / ol - 1) * 100 if ol > 0 else 0
        if width < cfg["min_range_pct"] or width > cfg["max_range_pct"]:
            counts["narrow_or_wide"] += 1
            continue
        pc = last.get("prev_close")
        if pc:
            chg = (last["c"] / pc - 1) * 100
            if abs(chg) >= cfg["circuit_pct"] - cfg["circuit_guard_pct"]:
                counts["near_circuit"] += 1
                continue
        counts["scanned"] += 1
        if k0:
            counts["late_started"] += 1
        scanned.append(({b["k"]: b for b in bars}, last["c"]))
        # per-bar volume from cumulative volume; range average is cumulative-at-range-end / n_range
        avg_rng_vol = rng[-1]["vol"] / n_range if n_range else 0.0
        for i in range(n_range, len(bars)):
            b = bars[i]
            if b["c"] > oh * (1 + cfg["min_breakout_pct"] / 100.0):
                bar_vol = b["vol"] - bars[i - 1]["vol"]
                if cfg["vol_mult"] and avg_rng_vol > 0 and bar_vol < cfg["vol_mult"] * avg_rng_vol:
                    counts["no_volume"] = counts.get("no_volume", 0) + 1
                    continue
                closes_at = b["start"] + dt.timedelta(minutes=BAR_MIN)
                hits.append({"symbol": sym, "price": round(b["c"], 2), "at": closes_at.strftime("%H:%M"),
                             "or_high": round(oh, 2), "or_low": round(ol, 2),
                             "pct_over": round((b["c"] / oh - 1) * 100, 2),
                             "range_pct": round(width, 2),
                             "range_start": rng[0]["start"].strftime("%H:%M"),
                             "_k": b["k"],
                             "last": round(last["c"], 2),
                             "since_pct": round((last["c"] / b["c"] - 1) * 100, 2),
                             "peak_pct": round((max(x["h"] for x in bars[i:]) / b["c"] - 1) * 100, 2),
                             "vol_x": None if avg_rng_vol <= 0 else round(bar_vol / avg_rng_vol, 1),
                             "turnover": round(last["turn"]),
                             "chg_vs_prev_close": None if not pc else round((last["c"] / pc - 1) * 100, 2),
                             "_t": closes_at})
                break
    hits.sort(key=lambda h: (h["_t"], h["symbol"]))
    # Baseline per breakout bar: median move of ALL scanned stocks from that same bar to the cutoff.
    base = {}
    for k in {h["_k"] for h in hits}:
        ch = [lc / m[k]["c"] - 1 for m, lc in scanned if k in m and m[k]["c"] > 0]
        base[k] = round(statistics.median(ch) * 100, 2) if ch else 0.0
    for h in hits:
        h["base_pct"] = base[h["_k"]]
        h["excess_pct"] = round(h["since_pct"] - h["base_pct"], 2)
        del h["_t"], h["_k"]
    counts["hits"] = len(hits)
    return hits, counts


# ------------------------------------------------------------------ commands

def trading_day_ok(day, cfg):
    return day.weekday() in cfg["trading_days"]


def cmd_record(a, cfg):
    open_t, close_t = hhmm(cfg["open"]), hhmm(cfg["close"])
    print("Recording every %ds to %s. Ctrl-C to stop." % (a.every, cfg["store"]), file=sys.stderr)
    while True:
        now = now_npt()
        if trading_day_ok(now.date(), cfg) and open_t <= now.time() < close_t:
            try:
                rows = normalise(fetch_source(a.source))
                if rows:
                    append_snapshot(cfg["store"], now, rows)
                    print("%s  %d symbols" % (now.strftime("%H:%M:%S"), len(rows)), file=sys.stderr)
                else:
                    print("%s  empty response" % now.strftime("%H:%M:%S"), file=sys.stderr)
            except Exception as e:  # keep recording through a bad minute
                print("%s  fetch failed: %s" % (now.strftime("%H:%M:%S"), e), file=sys.stderr)
        elif a.once:
            print("Outside trading hours (%s NPT)." % now.strftime("%a %H:%M"), file=sys.stderr)
        if a.once:
            return 0
        time.sleep(a.every)


def cmd_inspect(a, cfg):
    """Fetch once and show the raw rows next to what the scanner understood."""
    raw = fetch_source(a.source)
    rows = raw
    if isinstance(raw, dict):
        for k in ("data", "result", "content", "liveMarket"):
            if isinstance(raw.get(k), list):
                rows = raw[k]
                break
    rows = rows if isinstance(rows, list) else []
    print("Fetched %d raw rows." % len(rows))
    for r in rows[:2]:
        print("RAW   :", json.dumps(r, default=str)[:600])
    norm = normalise(raw)
    print("Understood %d rows." % len(norm))
    for r in norm[:2]:
        print("MAPPED:", r)
    if rows and not norm:
        print("None understood: send me the RAW lines above so I can fix the field names.")
    elif norm:
        missing = [k for k in ("volume", "turnover", "prev_close") if not any(r[k] for r in norm)]
        if missing:
            print("Warning: no values for %s. Volume/liquidity filters will not work until fixed; send me the RAW lines."
                  % ", ".join(missing))
    return 0 if norm else 1


def run_scan(snaps, day, cutoff, or_minutes, cfg):
    open_t, close_t = hhmm(cfg["open"]), hhmm(cfg["close"])
    n_range = or_minutes // BAR_MIN
    bars = build_bars(snaps, day, open_t, close_t, cutoff)
    return orb_scan(bars, n_range, cfg)


def cmd_scan(a, cfg):
    if a.or_minutes % BAR_MIN or not (5 <= a.or_minutes <= 120):
        print("--or-minutes wants a multiple of 5 from 5 to 120 (15 or 30).", file=sys.stderr)
        return 1
    now = now_npt()
    day = dt.date.fromisoformat(a.day) if a.day else now.date()
    if not trading_day_ok(day, cfg):
        print("No session on %s (not in trading_days)." % day, file=sys.stderr)
        return 1
    open_t = hhmm(cfg["open"])
    cutoff = dt.datetime.combine(day, hhmm(a.at)) if a.at else now.replace(second=0, microsecond=0)
    first_close = dt.datetime.combine(day, open_t) + dt.timedelta(minutes=a.or_minutes + BAR_MIN)
    if cutoff < first_close:
        print("Too early: the %d-minute range runs to %s; first bar after it closes %s. Run after that."
              % (a.or_minutes, (first_close - dt.timedelta(minutes=BAR_MIN)).strftime("%H:%M"),
                 first_close.strftime("%H:%M")), file=sys.stderr)
        return 1
    snaps = read_snapshots(cfg["store"], day)
    if not snaps:
        print("No snapshots recorded for %s in %s. Run `record` during the session first." % (day, cfg["store"]),
              file=sys.stderr)
        return 1
    hits, c = run_scan(snaps, day, cutoff, a.or_minutes, cfg)
    if a.json:
        print(json.dumps({"day": str(day), "as_of": cutoff.strftime("%H:%M"), "counts": c, "hits": hits}, indent=1))
    else:
        print("NEPSE ORB scan %s %s NPT: %d-minute range, %d-minute bars from %d snapshots."
              % (day, cutoff.strftime("%H:%M"), a.or_minutes, BAR_MIN, len(snaps)))
        print("%d symbols seen; %d scanned (%d of them began trading after the open, range taken from their first trade)."
              % (c["symbols"], c["scanned"], c["late_started"]))
        print("Skipped: %d with gaps in their bars, %d first traded over %d min after the open, %d illiquid, "
              "%d range outside %.1f-%.1f%%, %d near circuit."
              % (c["unusable"], c["too_late"], cfg["max_late_start_min"], c["illiquid"], c["narrow_or_wide"],
                 cfg["min_range_pct"], cfg["max_range_pct"], c["near_circuit"]))
        print("%d matched an opening-range breakout rule (matched a rule, not a buy):" % len(hits))
        for h in hits[:a.show]:
            late = "" if h["range_start"] == cfg["open"] else "  [range from %s]" % h["range_start"]
            print("  %s  %-8s Rs %9.2f  range %.2f-%.2f  +%.2f%% over  turnover Rs %s  | latest %.2f (%+.2f%%; market %+.2f%%; peak %+.2f%%)%s"
                  % (h["at"], h["symbol"], h["price"], h["or_low"], h["or_high"], h["pct_over"],
                     format(h["turnover"], ","), h["last"], h["since_pct"], h["base_pct"], h["peak_pct"], late))
        if len(hits) > a.show:
            print("  ... %d more (use --show 200 to list all)" % (len(hits) - a.show))
        if hits:
            ch = sorted(h["since_pct"] for h in hits)
            up = sum(1 for x in ch if x > 0)
            print("Since breakout to %s: %d up, %d down, %d flat; median %+.2f%%, best %+.2f%%, worst %+.2f%%."
                  % (cutoff.strftime("%H:%M"), up, sum(1 for x in ch if x < 0), sum(1 for x in ch if x == 0),
                     ch[len(ch) // 2], ch[-1], ch[0]))
            ex = sorted(h["excess_pct"] for h in hits)
            print("Versus the market baseline (median of all %d scanned stocks, from the same bar to %s): "
                  "%d beat it, %d lagged it, %d equal; median excess %+.2f%%."
                  % (c["scanned"], cutoff.strftime("%H:%M"), sum(1 for x in ex if x > 0),
                     sum(1 for x in ex if x < 0), sum(1 for x in ex if x == 0), ex[len(ex) // 2]))
            print("One day is one sample. A signal is only interesting if hits beat the baseline on most days.")
    if a.write:
        with open(a.write, "w") as f:
            json.dump({"day": str(day), "as_of": cutoff.strftime("%H:%M"), "hits": hits}, f, indent=1)
    return 0 if hits else 1


def synth_session(day, cfg, seed=7):
    """Synthetic NEPSE-shaped session: 60 symbols, 1 snapshot/min. Planted breakouts
    BRK1 (breaks at 11:30), BRK2 (breaks 11:50), plus FADE (breaks then reverses back),
    ILLQ (thin turnover), CIRC (near +15% band) and 50 random walkers."""
    import random
    rnd = random.Random(seed)
    open_t = hhmm(cfg["open"])
    start = dt.datetime.combine(day, open_t)
    syms = {}

    def path(base, drift_fn, noise):
        p, out = base, []
        for m in range(0, 180):
            p = max(1.0, p * (1 + drift_fn(m) + rnd.uniform(-noise, noise)))
            out.append(round(p, 1))
        return out

    flat = lambda m: 0.0
    # surge = (first_minute, last_minute, multiplier) of extra volume on the breakout
    syms["BRK1"] = (path(500, lambda m: 0.0015 if m >= 30 else 0.0, 0.0012), 500, 5e6, (28, 40, 6))
    syms["BRK2"] = (path(300, lambda m: 0.0016 if m >= 50 else 0.0, 0.0012), 300, 5e6, (48, 60, 6))
    syms["FADE"] = (path(250, lambda m: 0.003 if 20 <= m < 30 else (-0.003 if m >= 30 else 0.0), 0.0012),
                    250, 5e6, (18, 32, 6))
    syms["ILLQ"] = (path(120, lambda m: 0.002 if m >= 25 else 0.0, 0.0012), 120, 1e4, (23, 35, 6))
    syms["CIRC"] = (path(400, lambda m: 0.0025 if m >= 20 else 0.0, 0.0012), 400 / 1.138, 5e6, (18, 40, 6))
    syms["NOVOL"] = (path(220, lambda m: 0.0015 if m >= 30 else 0.0, 0.002), 220, 5e6, None)  # drifts up, no volume
    for i in range(50):
        syms["W%02d" % i] = (path(rnd.uniform(100, 900), flat, 0.0025), None, 3e6, None)
    snaps = []
    cum = {s: 0.0 for s in syms}
    for m in range(0, 180):
        t = start + dt.timedelta(minutes=m)
        rows = []
        for s, (ps, prev, tv, surge) in syms.items():
            p = ps[m]
            base = tv / p / 180
            mult = surge[2] if surge and surge[0] <= m < surge[1] else 1.0
            cum[s] += base * mult
            rows.append({"symbol": s, "ltp": p, "volume": round(cum[s]),
                         "turnover": round(cum[s] * p), "prev_close": prev or ps[0],
                         "high": None, "low": None})
        snaps.append((t, rows))
    return snaps


def cmd_demo(a, cfg):
    day = dt.date(2026, 10, 26)  # a Monday
    snaps = synth_session(day, cfg)
    for label, hhmm_cut in (("12:00", "12:00"), ("13:00", "13:00")):
        cutoff = dt.datetime.combine(day, hhmm(hhmm_cut))
        hits, c = run_scan(snaps, day, cutoff, a.or_minutes, cfg)
        print("== demo (synthetic data) scan at %s NPT, %d-minute range ==" % (label, a.or_minutes))
        print("   %s" % c)
        for h in hits:
            print("   %s %-5s %8.1f  range %.1f-%.1f  +%.2f%% over" %
                  (h["at"], h["symbol"], h["price"], h["or_low"], h["or_high"], h["pct_over"]))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.json")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="poll live market and append snapshots during the session")
    r.add_argument("--source", required=True, help="URL returning live-market JSON (e.g. a local NEPSE API server)")
    r.add_argument("--every", type=int, default=60)
    r.add_argument("--once", action="store_true")
    i = sub.add_parser("inspect", help="fetch once and print raw vs understood rows (field-name check)")
    i.add_argument("--source", required=True, help="URL, or py:nepse_data_api")
    s = sub.add_parser("scan", help="scan recorded snapshots for opening-range breakouts")
    s.add_argument("--at", help="HH:MM NPT cutoff (default: now)")
    s.add_argument("--day", help="YYYY-MM-DD (default: today)")
    s.add_argument("--or-minutes", type=int, default=15)
    s.add_argument("--show", type=int, default=200)
    s.add_argument("--json", action="store_true")
    s.add_argument("--write", help="write hits to this JSON file")
    d = sub.add_parser("demo", help="run on synthetic data")
    d.add_argument("--or-minutes", type=int, default=15)
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    return {"record": cmd_record, "scan": cmd_scan, "demo": cmd_demo, "inspect": cmd_inspect}[a.cmd](a, cfg)


if __name__ == "__main__":
    sys.exit(main())
