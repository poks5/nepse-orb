# nepse-orb: opening-range-breakout scanner for NEPSE

Adapted from the `trader-scanner` idea in Jake Schincariol's day-trader-skill (MIT).
Python 3.8+, standard library only.

**Status: logic tested on synthetic data only. The live NEPSE feed has NOT been tested.**
The build sandbox cannot reach nepalstock.com, so the first real test is your first live session.

## Why it records its own bars
NEPSE has no public intraday history. `record` polls the live market once a minute and stores
snapshots in `snapshots.jsonl`. `scan` builds 5-minute bars from them. So you must have the
recorder running from 11:00; the scan cannot see anything before that.

## Run it
1. Start a NEPSE data server on your PC (any unofficial client with a `/market/live` style
   JSON endpoint; e.g. NEPSE API Pro: `api-nepse`, then `http://localhost:8080/api/v1/market/live`).
2. Before 11:00 NPT (trading days are Mon-Fri by default, check the current NEPSE notice):
   `python3 nepse_orb.py record --source http://localhost:8080/api/v1/market/live`
3. After 11:20 (15-min range) or 11:35 (30-min range), in another terminal:
   `python3 nepse_orb.py scan --or-minutes 15`
4. `python3 nepse_orb.py demo` runs on synthetic data with no network.
5. `python3 -m unittest -v` runs the tests.

If `record` prints "empty response" or the scan sees 0 symbols, the JSON field names differ from
the ones in `FIELDS` at the top of `nepse_orb.py`. Open the URL in a browser, then add the names.

## The rule
- Opening range = high/low of the first N minutes after 11:00 (default 15).
- A hit = first completed 5-minute bar that CLOSES above range high by at least `min_breakout_pct`
  (0.3%), on a bar volume of at least `vol_mult` (1.5x) the average range bar.
- Skipped: bars with gaps, turnover below `min_turnover`, range narrower than 0.3% or wider than 8%,
  and stocks within 1.5 points of the 15% daily band (can't be bought at the edge).

## Config (`config.json`, optional)
Any key in `DEFAULTS` at the top of the script. Examples: `{"trading_days": [6,0,1,2,3], "min_turnover": 1000000}`.
Defaults are untuned guesses. Tune them against your own recorded sessions.

## Limits you should know
- Bar high/low come from ~1/min price samples, not ticks. The range is approximate.
- The FADE case in the demo shows a flagged breakout can reverse: a hit is a match, not a buy.
- Same-day selling may not be possible on NEPSE (T+2, no shorting; verify with your broker).
  Treat hits as entries for multi-day holds, and log outcomes before risking money.
- Unofficial data sources: no accuracy guarantee, and commercial use needs a NEPSE licence.
- Not financial advice.
