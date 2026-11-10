"""London (LSE) and Frankfurt (Xetra) support across the pack.

Fixture in tests/fixtures/markets/:
SAP.DE-5d-5m-2026-11-10.json.gz  SAP on Xetra, Yahoo chart JSON v8, range=5d interval=5m,
                                 fetched 2026-11-10 11:07 UTC. Holds the Mon Nov 9 session:
                                 opening range 9:00-9:15 high 215.50 low 213.65, and the
                                 10:05 bar closing at 215.70 (the first close above it).
"""

import datetime
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import load, FIX, ROOT

scan = load("trader-scanner", "scan")
entry = load("trader-entry", "entry")
exitp = load("trader-exit", "exit")
tm = load("trader-timemachine", "timemachine")
check = load("trader-risk", "check")
alerts = load("trader-alerts", "alerts")
size = load("trader-budget", "size")
ticket = load("trader-broker", "ticket")
news = load("trader-news", "news")

TOOLS = [scan, entry, exitp, tm, check, alerts, size]
BLOCK_FILES = ["trader-scanner/scan.py", "trader-entry/entry.py", "trader-exit/exit.py",
               "trader-timemachine/timemachine.py", "trader-risk/check.py", "trader-alerts/alerts.py",
               "trader-budget/size.py"]
SAP = os.path.join(FIX, "markets", "SAP.DE-5d-5m-2026-11-10.json.gz")
DT = datetime.datetime
D = datetime.date


def block(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        text = fh.read()
    a = text.index("# ---------------------------------------------------------------- markets\n")
    b = text.index("# ---------------------------------------------------------------- end of markets\n")
    return text[a:b]


def run(mod, argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = mod.main(argv)
    return code, out.getvalue()


class Reset(unittest.TestCase):
    def tearDown(self):
        for m in TOOLS:
            m.use_market("us")


class TheBlock(Reset):
    def test_every_copy_is_the_same(self):
        first = block(BLOCK_FILES[0])
        for path in BLOCK_FILES[1:]:
            self.assertEqual(block(path), first, "%s has a different markets block" % path)

    def test_a_suffix_picks_the_market(self):
        self.assertEqual(scan.market_of("VOD.L"), "london")
        self.assertEqual(scan.market_of("BT-A.L"), "london")
        self.assertEqual(scan.market_of("SAP.DE"), "frankfurt")
        self.assertEqual(scan.market_of("1COV.DE"), "frankfurt")
        for us in ("TSLA", "BRK.B", "L", "DE", ""):
            self.assertEqual(scan.market_of(us), "us", us)
        self.assertEqual(scan.market_key("LSE"), "london")
        self.assertEqual(scan.market_key("xetra"), "frankfurt")
        with self.assertRaises(ValueError):
            scan.market_key("tokyo")

    def test_new_york_is_the_default_and_unchanged(self):
        self.assertEqual(scan.MARKET, "us")
        self.assertEqual((scan.OPEN, scan.CLOSE, scan.EARLY), (datetime.time(9, 30), datetime.time(16), datetime.time(13)))
        self.assertEqual(scan.eastern_offset(DT(2026, 7, 1, 12)), -4)
        self.assertEqual(scan.eastern_offset(DT(2026, 12, 1, 12)), -5)
        self.assertEqual(scan.localize("10:15 ET New York NYSE $5.00"), "10:15 ET New York NYSE $5.00")

    def test_london_clock_follows_the_eu_rule(self):
        scan.use_market("london")
        # BST from 01:00 UTC on the last Sunday of March (2026-03-29) to the last Sunday of October (2026-10-25)
        self.assertEqual(scan.eastern_offset(DT(2026, 3, 29, 0, 59)), 0)
        self.assertEqual(scan.eastern_offset(DT(2026, 3, 29, 1, 0)), 1)
        self.assertEqual(scan.eastern_offset(DT(2026, 10, 25, 0, 59)), 1)
        self.assertEqual(scan.eastern_offset(DT(2026, 10, 25, 1, 0)), 0)
        self.assertEqual(scan.to_eastern(DT(2026, 11, 10, 8, 0)), DT(2026, 11, 10, 8, 0))
        self.assertEqual(scan.eastern_to_utc(DT(2026, 7, 1, 8, 0)), DT(2026, 7, 1, 7, 0))

    def test_frankfurt_clock(self):
        scan.use_market("frankfurt")
        self.assertEqual(scan.to_eastern(DT(2026, 11, 10, 8, 0)), DT(2026, 11, 10, 9, 0))
        self.assertEqual(scan.to_eastern(DT(2026, 7, 1, 7, 0)), DT(2026, 7, 1, 9, 0))
        for local in (DT(2026, 1, 5, 9), DT(2026, 6, 5, 17, 30)):
            self.assertEqual(scan.to_eastern(scan.eastern_to_utc(local)), local)

    def test_sessions_and_holidays(self):
        scan.use_market("london")
        self.assertEqual(scan.session(D(2026, 11, 10)), (datetime.time(8), datetime.time(16, 30)))
        self.assertEqual(scan.session(D(2026, 12, 24)), (datetime.time(8), datetime.time(12, 30)))
        self.assertIsNone(scan.session(D(2026, 12, 28)))       # Boxing Day, substitute day
        self.assertIsNone(scan.session(D(2026, 4, 6)))         # Easter Monday
        self.assertEqual(scan.trading_day(D(2026, 8, 31))[1], "LSE holiday: Summer bank holiday")
        self.assertIsNotNone(scan.session(D(2026, 11, 26)))    # US Thanksgiving: London trades
        scan.use_market("frankfurt")
        self.assertEqual(scan.session(D(2026, 11, 10)), (datetime.time(9), datetime.time(17, 30)))
        self.assertIsNone(scan.session(D(2026, 12, 24)))       # Xetra is shut on Christmas Eve
        self.assertIsNone(scan.session(D(2026, 5, 1)))         # Labour Day
        self.assertEqual(scan.SESSION_BARS, 102)

    def test_entry_window_defaults_and_config(self):
        self.assertEqual(scan.market_times({}, "london"),
                         {"no_entries_before": "08:15", "no_entries_after": "16:00", "flat_by": "16:25"})
        self.assertEqual(scan.market_times({}, "frankfurt")["flat_by"], "17:25")
        cfg = {"flat_by": "15:50", "markets": {"london": {"flat_by": "16:10"}}}
        self.assertEqual(scan.market_times(cfg, "london")["flat_by"], "16:10")
        self.assertEqual(scan.market_times(cfg, "frankfurt")["flat_by"], "17:25")   # the New York key is not used
        self.assertEqual(scan.market_times(cfg, "us")["flat_by"], "15:50")

    def test_wording_and_prices(self):
        scan.use_market("london")
        self.assertEqual(scan.localize("It is 10:15 ET in New York: NYSE. $421.50 over $1,234.5"),
                         "It is 10:15 London time in London: LSE. 421.50p over 1,234.5p")
        scan.use_market("frankfurt")
        self.assertEqual(scan.localize("at $215.70"), "at €215.70")
        size.use_market("frankfurt")
        self.assertEqual(size.localize("max loss $5"), "max loss $5")   # the budget's $ are dollars


class Scanner(Reset):
    def test_yahoo_symbols_keep_their_suffix(self):
        self.assertEqual(scan.to_yahoo("BRK.B"), "BRK-B")
        self.assertEqual(scan.to_yahoo("bt-a.l"), "BT-A.L")
        self.assertEqual(scan.from_yahoo("BRK-B"), "BRK.B")
        self.assertEqual(scan.from_yahoo("BT-A.L"), "BT-A.L")

    def test_starter_lists(self):
        for mk, suffix, n in (("london", ".L", 100), ("frankfurt", ".DE", 42)):
            syms = scan.symbols_from_file(scan.bundled_universe(mk))
            self.assertEqual(len(syms), n, mk)
            self.assertTrue(all(s.endswith(suffix) and scan.market_of(s) == mk for s in syms), mk)
            self.assertEqual(len(set(syms)), n)

    def test_universe_abroad_copies_the_starter_list(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        out = os.path.join(d, "u.txt")
        code, text = run(scan, ["universe", "--market", "frankfurt", "--out", out])
        self.assertEqual(code, 0)
        self.assertIn("42 large, liquid stocks", text)
        self.assertEqual(scan.symbols_from_file(out)[:2], ["ADS.DE", "AIR.DE"])

    def test_bare_symbols_get_the_market_suffix(self):
        scan.use_market("london")
        a = type("A", (), {"symbols": "VOD, bp.l", "watchlist": None, "universe": None, "limit": None})()
        self.assertEqual(scan.read_symbols(a), ["VOD.L", "BP.L"])
        a.symbols = "VOD.L,SAP.DE"                     # a Frankfurt ticker in a London scan
        with self.assertRaises(scan.ScanError):
            scan.read_symbols(a)

    def _bars(self, day, slots):
        return [{"start": scan.slot(day, k), "c": 100.0 + k, "h": 101.0 + k, "l": 99.0 + k, "v": 1}
                for k in slots]

    def test_usable_allows_small_gaps_abroad_only(self):
        day = D(2026, 11, 10)
        scan.use_market("london")
        cutoff = scan.slot(day, 33)                    # 33 slots, 3 in the range, 30 after
        full = list(range(33))
        gappy = [k for k in full if k not in (10, 20, 25)]
        self.assertTrue(scan.usable(self._bars(day, full), day, cutoff, 3))
        self.assertTrue(scan.usable(self._bars(day, gappy), day, cutoff, 3))         # 3 of 30 missing
        many = [k for k in full if k < 3 or k % 4]                                    # 8 of 30 missing
        self.assertFalse(scan.usable(self._bars(day, many), day, cutoff, 3))
        self.assertFalse(scan.usable(self._bars(day, [0, 2] + full[3:]), day, cutoff, 3))  # a hole in the range
        scan.use_market("us")
        cutoff = scan.slot(day, 33)
        self.assertFalse(scan.usable(self._bars(day, gappy), day, cutoff, 3))        # New York: no gaps

    def test_orb_hit_counts_slots_not_list_positions(self):
        scan.use_market("london")
        day = D(2026, 11, 10)
        bars = self._bars(day, [0, 1, 2, 4, 6])
        for b in bars:
            b["c"], b["h"] = 100.0, 100.5
        bars[4]["c"] = 101.0                           # slot 6 closes above the 100.5 range high
        hit = scan.orb_hit(bars, 3)
        self.assertEqual((hit["bar_index"], hit["bar_start"], hit["at"]), (6, "08:30", "8:35 AM"))


class EntryExitReplay(Reset):
    def test_entry_on_xetra_from_the_fixture(self):
        code, text = run(entry, ["check", "SAP.DE", "--offline", SAP, "--now", "2026-11-09 10:30"])
        self.assertEqual(code, 0)
        self.assertIn("SAP.DE  Mon 2026-11-09 10:30 Frankfurt time", text)
        self.assertIn("Opening range 9:00-9:15: high €215.50, low €213.65 (3 of 3 bars).", text)
        self.assertIn("TRIGGERED 10:10 AM at €215.70: the 10:05 bar (bar 13)", text)
        self.assertNotIn("$", text)

    def test_entry_before_the_xetra_open(self):
        code, text = run(entry, ["check", "SAP.DE", "--offline", SAP, "--now", "2026-11-09 08:30"])
        self.assertEqual(code, 1)
        self.assertIn("Xetra opens at 9:00", text)

    def test_entry_and_exit_files_carry_the_market(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        ej, pj = os.path.join(d, "entry.json"), os.path.join(d, "plan.json")
        run(entry, ["check", "SAP.DE", "--offline", SAP, "--now", "2026-11-09 10:30", "--write", ej])
        with open(ej) as fh:
            e = json.load(fh)
        self.assertEqual((e["market"], e["time"], e["price"]), ("frankfurt", "10:10 AM", 215.7))
        code, text = run(exitp, ["plan", "--from", ej, "--now", "2026-11-09 10:30", "--write", pj,
                                 "--config", os.path.join(d, "none.json")])
        self.assertEqual(code, 0)
        self.assertIn("Stop          €214.57  opening-range midpoint", text)
        self.assertIn("Flat by       17:25 Frankfurt time", text)
        with open(pj) as fh:
            plan = json.load(fh)
        self.assertEqual((plan["market"], plan["flat_by"], plan["stop"], plan["target"]),
                         ("frankfurt", "17:25", 214.57, 217.96))

    def test_exit_london_flat_by_on_a_half_day(self):
        code, text = run(exitp, ["plan", "--symbol", "VOD.L", "--entry", "120", "--or-high", "119.5",
                                 "--or-low", "118", "--date", "2026-12-24", "--now", "2026-12-24 09:00",
                                 "--config", "/nonexistent.json"])
        self.assertEqual(code, 0)
        self.assertIn("Flat by       12:25 London time", text)
        self.assertIn("early close", text)

    def test_replay_needs_the_xetra_session_shape(self):
        tm.use_market("frankfurt")
        self.assertEqual((tm.OPEN_MIN, tm.CLOSE_MIN, tm.SESSION_BARS), (540, 1050, 102))
        self.assertEqual(tm.yahoo_symbol("SAP.DE"), "SAP.DE")
        self.assertEqual(tm.yahoo_symbol("BRK.B"), "BRK-B")


class SizingAndRisk(Reset):
    PLAN = {"symbol": "WPP.L", "side": "buy", "date": "2026-11-10", "setup": "orb", "entry": 421.50,
            "stop": 417.75, "target": 429.00, "entry_time": "8:30 AM", "flat_by": "16:25"}
    CFG = {"budget": 50, "max_loss_per_trade_usd": 5, "cash_buffer_pct": 10, "max_position_pct": 75}
    K = 1.3431 / 100

    def test_london_sized_in_dollars(self):
        s = size.size_trade(self.PLAN, self.CFG, k_usd=self.K)
        got = {l["name"]: l["shares"] for l in s["limits"]}
        self.assertEqual(got, {"max loss": 99, "position cap": 6, "max order": 706, "cash": 7})
        self.assertEqual((s["qty"], s["bound_by"], s["cost"], s["max_loss"]), (6, "position cap", 33.97, 0.3))
        order = size.bracket_order(s)
        self.assertEqual(order["stop_loss_stop_price"], "417.75")
        self.assertEqual(order["plan"]["currency"], "GBp")
        self.assertIn("6 x 3.75p = $0.30", order["why"])
        report = size.plan_report(s, order)
        self.assertIn("Alpaca trades US stocks only", report)
        self.assertIn("421.50p", report)

    def test_no_rate_no_size(self):
        size.use_market("london")
        with self.assertRaises(size.SizeError):
            size.fx_for("london", {})
        self.assertEqual(size.fx_for("london", {"fx": {"GBPUSD": 1.3431}})[0], self.K)
        self.assertEqual(size.fx_for("us", {})[0], 1.0)

    def _order(self, qty=6):
        s = size.size_trade(self.PLAN, self.CFG, k_usd=self.K)
        o = size.bracket_order(s)
        o["qty"] = str(qty)
        return o

    def test_risk_passes_a_london_order_in_dollars(self):
        now = DT(2026, 11, 10, 11, 0)          # 11:00 London (UTC in November)
        res = check.check([self._order()], self.CFG, now=now, fx={"GBPUSD": (1.3431, now)})
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual((r["market"], r["max_loss"], r["value_usd"]), ("london", 0.3, 33.97))
        self.assertTrue(any("GBPUSD" in x for x in r["info"]))

    def test_risk_blocks_without_a_rate(self):
        res = check.check([self._order()], self.CFG, now=DT(2026, 11, 10, 11, 0))
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("fx:") for x in res["results"][0]["reasons"]))

    def test_risk_uses_the_london_window(self):
        fx = {"GBPUSD": (1.3431, None)}
        early = check.check([self._order()], self.CFG, now=DT(2026, 11, 10, 8, 5), fx=fx)   # 8:05 London
        self.assertIn("window: no new entries before 08:15 London (it is 08:05)", early["results"][0]["reasons"])
        late = check.check([self._order()], self.CFG, now=DT(2026, 11, 10, 16, 10), fx=fx)  # after 16:00
        self.assertTrue(any("no new entries after 16:00 London" in x for x in late["results"][0]["reasons"]))
        # 10:00 New York is 15:00 London: the New York window does not apply to a London order
        ok = check.check([self._order()], self.CFG, now=DT(2026, 11, 10, 15, 0), fx=fx)
        self.assertEqual(ok["results"][0]["verdict"], "PASS", ok["results"][0]["reasons"])

    def test_risk_loss_limit_in_dollars(self):
        big = self._order(qty=120)              # 120 x 3.75p = 450p = $6.04
        res = check.check([big], dict(self.CFG, budget=5000), now=DT(2026, 11, 10, 11, 0),
                          fx={"GBPUSD": (1.3431, None)})
        self.assertTrue(any(x.startswith("loss:") and "$6.04" in x for x in res["results"][0]["reasons"]))

    def test_risk_accepts_abroad_tickers(self):
        for sym in ("VOD.L", "BT-A.L", "SAP.DE", "1COV.DE"):
            self.assertTrue(check.ABROAD.match(sym), sym)
        self.assertFalse(check.ABROAD.match("SAP.PA"))

    def test_fx_from_prices_json(self):
        fx = check.load_fx({"fx": {"GBPUSD": 1.3431}, "fx_detail": {"GBPUSD": {"time": "2026-11-10T10:51:00Z"}}})
        self.assertEqual(fx, {"GBPUSD": (1.3431, DT(2026, 11, 10, 10, 51))})


class BrokerAlertsNews(Reset):
    def test_alpaca_will_not_take_a_london_order(self):
        o = {"symbol": "WPP.L", "side": "buy", "qty": "6", "type": "market", "time_in_force": "day"}
        ready, label, _ = ticket.status_of(o, {}, {}, "2026-11-10", {"broker": "alpaca"})
        self.assertFalse(ready)
        self.assertTrue(label.startswith("NOT FOR ALPACA"))
        self.assertIn("London Stock Exchange", label)
        ready, label, _ = ticket.status_of(o, {}, {}, "2026-11-10", {"broker": "ibkr"})
        self.assertFalse(label.startswith("NOT FOR ALPACA"))
        self.assertIsNone(ticket.abroad("TSLA"))

    def test_plan_alerts_ring_on_london_time(self):
        plan = {"symbol": "VOD.L", "stop": 115.0, "target": 125.0, "date": "2026-11-10"}
        rules = alerts.rules_from_plan(plan, "15:55")
        flat = [r for r in rules if r["type"] == "time"][0]
        self.assertEqual((flat["at"], flat["market"]), ("16:25", "london"))
        us_status = alerts.market_status(DT(2026, 11, 10, 16, 26))
        rings, _ = alerts.evaluate([flat], {}, us_status, {}, DT(2026, 11, 10, 16, 26))   # 16:26 London
        self.assertEqual(len(rings), 1)
        self.assertIn("London", rings[0]["message"])
        self.assertEqual(alerts.MARKET, "us")
        rings, _ = alerts.evaluate([flat], {}, us_status, {}, DT(2026, 11, 10, 16, 20))
        self.assertEqual(rings, [])

    def test_stop_alert_in_pence(self):
        rule = {"id": "vod-stop", "symbol": "VOD.L", "type": "stop", "price": 115.0}
        quotes = {"VOD.L": {"price": 114.9}}
        rings, _ = alerts.evaluate([rule], quotes, alerts.market_status(DT(2026, 11, 10, 11)), {},
                                   DT(2026, 11, 10, 11))
        self.assertIn("VOD.L is 114.90p: at or below your stop 115p", rings[0]["message"])

    def test_london_clock(self):
        code, text = run(alerts, ["clock", "--market", "london", "--now", "2026-11-10T11:00:00Z"])
        self.assertEqual(code, 0)
        self.assertIn("London 2026-11-10 11:00 (Tuesday): LSE OPEN", text)
        code, text = run(alerts, ["clock", "--market", "frankfurt", "--now", "2026-12-24T10:00:00Z"])
        self.assertEqual(code, 1)
        self.assertIn("Xetra closed", text)

    def test_news_keeps_the_suffix(self):
        self.assertEqual(news.yahoo_symbol("BT-A.L"), "BT-A.L")
        self.assertEqual(news.yahoo_symbol("BRK.B"), "BRK-B")
        self.assertTrue(news.is_abroad("SAP.DE"))


if __name__ == "__main__":
    unittest.main()
