import datetime as dt
import json
import os
import tempfile
import unittest

import nepse_orb as o

CFG = dict(o.DEFAULTS)
DAY = dt.date(2026, 10, 26)  # Monday


def scan(at, or_min=15, cfg=None, snaps=None):
    snaps = snaps or o.synth_session(DAY, CFG)
    cutoff = dt.datetime.combine(DAY, o.hhmm(at))
    return o.run_scan(snaps, DAY, cutoff, or_min, cfg or CFG)


class OrbTests(unittest.TestCase):
    def test_planted_breakouts_found_at_right_time(self):
        hits, _ = scan("12:30")
        by = {h["symbol"]: h for h in hits}
        self.assertIn("BRK1", by)
        self.assertIn("BRK2", by)
        self.assertLessEqual(by["BRK1"]["at"], "11:45")
        self.assertGreaterEqual(by["BRK2"]["at"], "11:50")
        self.assertGreater(by["BRK1"]["pct_over"], 0.3)

    def test_nothing_before_first_bar_closes(self):
        hits, c = scan("11:20")  # range 11:00-11:15, first bar after closes 11:20
        self.assertEqual(c["symbols"] > 0, True)
        self.assertNotIn("BRK2", {h["symbol"] for h in hits})

    def test_filters(self):
        hits, c = scan("12:30")
        syms = {h["symbol"] for h in hits}
        self.assertNotIn("ILLQ", syms)    # thin turnover
        self.assertNotIn("CIRC", syms)    # near the 15% band
        self.assertNotIn("NOVOL", syms)   # drifts up with no volume surge
        self.assertGreaterEqual(c["illiquid"], 1)
        self.assertGreaterEqual(c["near_circuit"], 1)
        self.assertGreaterEqual(c.get("no_volume", 0), 1)

    def test_random_walkers_rarely_flagged(self):
        hits, _ = scan("12:30")
        walkers = [h for h in hits if h["symbol"].startswith("W")]
        self.assertLessEqual(len(walkers), 3)

    def test_volume_filter_off_admits_novol(self):
        cfg = dict(CFG, vol_mult=0)
        hits, _ = scan("12:30", cfg=cfg)
        self.assertIn("NOVOL", {h["symbol"] for h in hits})

    def test_gap_in_bars_marks_symbol_unusable(self):
        snaps = o.synth_session(DAY, CFG)
        cut = dt.datetime.combine(DAY, dt.time(11, 20))
        snaps = [(t, [r for r in rows if not (r["symbol"] == "BRK1" and cut <= t < cut + dt.timedelta(minutes=5))])
                 for t, rows in snaps]
        hits, c = scan("12:30", snaps=snaps)
        self.assertNotIn("BRK1", {h["symbol"] for h in hits})
        self.assertGreaterEqual(c["unusable"], 1)

    def test_normalise_field_variants(self):
        rows = o.normalise({"data": [
            {"symbol": "nabil", "lastTradedPrice": "1,234.5", "totalTradeQuantity": 100, "previousClose": 1200},
            {"securitySymbol": "NICA", "ltp": 800, "totalTradedValue": 5e6},
            {"symbol": "BAD", "lastTradedPrice": None}]})
        self.assertEqual([r["symbol"] for r in rows], ["NABIL", "NICA"])
        self.assertEqual(rows[0]["ltp"], 1234.5)
        self.assertEqual(rows[1]["turnover"], 5e6)

    def test_real_nepse_data_api_rows(self):
        # rows copied from nepse-data-api get_stocks(date='2026-10-28')
        real = [{'id': None, 'businessDate': '2026-10-28', 'securityId': 2790, 'symbol': 'ACLBSL',
                 'openPrice': 845.0, 'highPrice': 850.0, 'lowPrice': 843.0, 'closePrice': 850.0,
                 'totalTradedQuantity': 1169, 'totalTradedValue': 988217.5, 'previousDayClosePrice': 842.0,
                 'lastUpdatedPrice': 851.5, 'totalTrades': 20},
                {'id': None, 'symbol': 'ADBL', 'highPrice': 338.9, 'lowPrice': 328.0, 'closePrice': 338.9,
                 'totalTradedQuantity': 60293, 'totalTradedValue': 20155310.4, 'previousDayClosePrice': 331.0}]
        rows = o.normalise(real)
        self.assertEqual([r["symbol"] for r in rows], ["ACLBSL", "ADBL"])
        self.assertEqual(rows[0]["ltp"], 851.5)        # lastUpdatedPrice preferred
        self.assertEqual(rows[1]["ltp"], 338.9)        # falls back to closePrice
        self.assertEqual(rows[0]["volume"], 1169)
        self.assertEqual(rows[0]["turnover"], 988217.5)
        self.assertEqual(rows[0]["prev_close"], 842.0)
        self.assertEqual(rows[0]["high"], 850.0)

    def test_store_roundtrip_and_scan(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.jsonl")
            for t, rows in o.synth_session(DAY, CFG):
                o.append_snapshot(p, t, rows)
            snaps = o.read_snapshots(p, DAY)
            self.assertEqual(len(snaps), 180)
            self.assertEqual(o.read_snapshots(p, DAY + dt.timedelta(days=1)), [])
            hits, _ = o.run_scan(snaps, DAY, dt.datetime.combine(DAY, dt.time(12, 30)), 15, CFG)
            self.assertIn("BRK1", {h["symbol"] for h in hits})

    def test_trading_day(self):
        self.assertTrue(o.trading_day_ok(DAY, CFG))
        self.assertFalse(o.trading_day_ok(dt.date(2026, 10, 31), CFG))  # Saturday

    def test_npt_offset(self):
        self.assertEqual(o.NPT.utcoffset(None), dt.timedelta(hours=5, minutes=45))


if __name__ == "__main__":
    unittest.main()
