#!/usr/bin/env python3
"""Price alerts and the NYSE clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # is the NYSE open? next open / close
    python3 alerts.py quote TSLA --write             # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every ticker in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by-close from trader/plan.json
    python3 alerts.py add --symbol TSLA --stop 358.32
    python3 alerts.py add --symbol TSLA --target 370.04
    python3 alerts.py add --at 15:55 --label "flat by close" --before-close
    python3 alerts.py add --symbol KO --pct 3
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-19 15:55" --offline tests/fixtures/trader-alerts
    python3 alerts.py list
    python3 alerts.py reset tsla-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    the NYSE opened today (rings once per trading day)
  time            the New York clock passed "at" (HH:MM) on a trading day
                  (rings once per trading day); "before_close": true moves it
                  3 hours earlier on a 1:00 p.m. early close, so a 15:55
                  "flat by close" rings at 12:55 on those days

A rule with "date" is only watched on that New York date (rules from a plan
are for the plan's day only). An above / below alert rings ONCE and then stays
quiet until you reset it, unless the rule has "rearm": true (it re-arms when
the price crosses back). A pct_move alert rings at most once per day.

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  yahoo   https://query2.finance.yahoo.com/v8/finance/chart/TSLA (falls back to
          query1 on a 429 or a 5xx; one retry with a pause on a 429; one
          request per ticker, half a second apart; the default)
  stooq   https://stooq.com/q/l/?s=tsla.us&f=sd2t2ohlcv&h&e=csv (Stooq's free
          CSV; it answered 404 on 2026-10-13, kept for when it is back)
  --offline DIR reads DIR/<source>-<TICKER>.json|csv (or .json.gz) instead.

NYSE calendar: the 2026 and 2027 holidays and 1:00 p.m. early closes as
published at https://www.nyse.com/markets/hours-calendars (read 2026-10-13).
Outside those years the clock says the holiday list is unknown.
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")



class AlertError(Exception):
    pass


# ---------------------------------------------------------------- time

PRICES_IN_MARKET_CURRENCY = True   # $ in this tool's output is a share price: shown as p or € abroad


# ---------------------------------------------------------------- markets
# The same block sits in scan.py, entry.py, exit.py, timemachine.py, check.py,
# alerts.py and size.py: each skill folder stands alone, and tests/test_markets.py
# keeps the copies equal. A ticker's suffix picks its market: VOD.L is London,
# SAP.DE is Frankfurt (Xetra), anything else is New York. Every clock time is the
# market's own local time. Calendars: NYSE from nyse.com (read 2026-10-13); London
# from the England and Wales bank holidays, with 12:30 half days on Christmas Eve
# and New Year's Eve; Xetra from Deutsche Boerse's trading calendar. London and
# Xetra compiled 2026-11-10: check the exchanges' own calendars before relying on them.
# delay: minutes Yahoo's free bars run behind the market (London and Xetra about 15).

NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
LSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-04-03": "Good Friday",
    "2026-04-06": "Easter Monday",
    "2026-05-04": "Early May bank holiday",
    "2026-05-25": "Spring bank holiday",
    "2026-08-31": "Summer bank holiday",
    "2026-12-25": "Christmas Day",
    "2026-12-28": "Boxing Day (substitute day)",
    "2027-01-01": "New Year's Day",
    "2027-03-26": "Good Friday",
    "2027-03-29": "Easter Monday",
    "2027-05-03": "Early May bank holiday",
    "2027-05-31": "Spring bank holiday",
    "2027-08-30": "Summer bank holiday",
    "2027-12-27": "Christmas Day (substitute day)",
    "2027-12-28": "Boxing Day (substitute day)",
}
LSE_EARLY_CLOSE = {  # 12:30 p.m. London
    "2026-12-24": "Christmas Eve",
    "2026-12-31": "New Year's Eve",
    "2027-12-24": "Christmas Eve",
    "2027-12-31": "New Year's Eve",
}
XETRA_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-04-03": "Good Friday",
    "2026-04-06": "Easter Monday",
    "2026-05-01": "Labour Day",
    "2026-12-24": "Christmas Eve",
    "2026-12-25": "Christmas Day",
    "2026-12-31": "New Year's Eve",
    "2027-01-01": "New Year's Day",
    "2027-03-26": "Good Friday",
    "2027-03-29": "Easter Monday",
    "2027-12-24": "Christmas Eve",
    "2027-12-31": "New Year's Eve",
}
MARKETS = {
    "us": {"name": "NYSE / Nasdaq", "exchange": "NYSE", "city": "New York", "zone": "ET",
           "dst": "us", "std": -5, "open": (9, 30), "close": (16, 0), "early": (13, 0),
           "suffix": "", "delay": 0, "currency": "USD", "fx": None, "per_unit": 1,
           "holidays": NYSE_HOLIDAYS, "early_closes": NYSE_EARLY_CLOSE},
    "london": {"name": "London Stock Exchange", "exchange": "LSE", "city": "London", "zone": "London time",
               "dst": "eu", "std": 0, "open": (8, 0), "close": (16, 30), "early": (12, 30),
               "suffix": ".L", "delay": 15, "currency": "GBp", "fx": "GBPUSD", "per_unit": 100,
               "holidays": LSE_HOLIDAYS, "early_closes": LSE_EARLY_CLOSE},
    "frankfurt": {"name": "Xetra (Frankfurt)", "exchange": "Xetra", "city": "Frankfurt", "zone": "Frankfurt time",
                  "dst": "eu", "std": 1, "open": (9, 0), "close": (17, 30), "early": None,
                  "suffix": ".DE", "delay": 15, "currency": "EUR", "fx": "EURUSD", "per_unit": 1,
                  "holidays": XETRA_HOLIDAYS, "early_closes": {}},
}
MARKET_ALIASES = {"ny": "us", "nyse": "us", "nasdaq": "us", "usa": "us", "lse": "london", "uk": "london",
                  "lon": "london", "xetra": "frankfurt", "fra": "frankfurt", "de": "frankfurt",
                  "germany": "frankfurt"}
CALENDAR_YEARS = (2026, 2027)
MARKET = "us"


def market_key(name):
    """'LSE', 'uk', 'London' -> 'london'. Raises ValueError for anything unknown."""
    key = str(name or "us").strip().lower()
    key = MARKET_ALIASES.get(key, key)
    if key not in MARKETS:
        raise ValueError("unknown market %r: use us, london or frankfurt" % name)
    return key


def market_of(symbol):
    """The market a ticker trades on, from its Yahoo suffix: VOD.L -> london, SAP.DE -> frankfurt."""
    s = str(symbol or "").strip().upper()
    for key, m in MARKETS.items():
        if m["suffix"] and s.endswith(m["suffix"].upper()) and len(s) > len(m["suffix"]):
            return key
    return "us"


def use_market(name):
    """Point the calendar, the clock and the wording at one market. Returns its settings."""
    global MARKET, OPEN, CLOSE, EARLY, HOLIDAYS, EARLY_CLOSES, OPEN_MIN, CLOSE_MIN, EARLY_MIN, SESSION_BARS
    MARKET = market_key(name)
    m = MARKETS[MARKET]
    OPEN, CLOSE = datetime.time(*m["open"]), datetime.time(*m["close"])
    EARLY = datetime.time(*m["early"]) if m["early"] else CLOSE
    HOLIDAYS, EARLY_CLOSES = m["holidays"], m["early_closes"]
    OPEN_MIN, CLOSE_MIN = OPEN.hour * 60 + OPEN.minute, CLOSE.hour * 60 + CLOSE.minute
    EARLY_MIN = EARLY.hour * 60 + EARLY.minute
    SESSION_BARS = (CLOSE_MIN - OPEN_MIN) // 5
    return m


def market_times(cfg=None, key=None):
    """no_entries_before / no_entries_after / flat_by for a market, as HH:MM local time.
    New York reads them from the top of trader/config.json. London and Frankfurt default
    to the same shape (15 minutes after the open, 30 and 5 minutes before the close) and
    read their own from config["markets"]["london"] (or "frankfurt")."""
    key = market_key(key or MARKET)
    cfg = cfg if isinstance(cfg, dict) else {}
    m = MARKETS[key]
    o = m["open"][0] * 60 + m["open"][1]
    c = m["close"][0] * 60 + m["close"][1]
    out = {"no_entries_before": "%02d:%02d" % divmod(o + 15, 60),
           "no_entries_after": "%02d:%02d" % divmod(c - 30, 60),
           "flat_by": "%02d:%02d" % divmod(c - 5, 60)}
    own = cfg if key == "us" else ((cfg.get("markets") or {}).get(key) or {})
    for k in out:
        if k in own:
            out[k] = own[k]
    return out


def localize(text):
    """New York wording -> the current market's. A no-op for New York."""
    if MARKET == "us" or not isinstance(text, str):
        return text
    m = MARKETS[MARKET]
    text = re.sub(r"\bET\b", m["zone"], text)
    text = text.replace("New York", m["city"]).replace("NYSE", m["exchange"])
    if globals().get("PRICES_IN_MARKET_CURRENCY"):
        # this tool prints share prices only, so $ means the market's own currency
        fmt = {"GBp": r"\1p", "EUR": "\u20ac\\1"}[m["currency"]]
        text = re.sub(r"\$(\d[\d,]*(?:\.\d+)?)", fmt, text)
    return text


def say(*args, **kw):
    """print(), in the current market's wording."""
    print(*(localize(a) for a in args), **kw)


def ampm_of(hm_pair):
    h, mi = hm_pair
    return "%d:%02d %s" % (h % 12 or 12, mi, "AM" if h < 12 else "PM")


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def _last_sunday(year, month):
    d = datetime.date(year + month // 12, month % 12 + 1, 1) - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


def eastern_offset(utc):
    """Hours from UTC for the current market's clock (New York by default).
    New York: -4 in daylight time, -5 otherwise; DST from 2:00 a.m. on the second
    Sunday of March to 2:00 a.m. on the first Sunday of November (US rule since 2007).
    London (0) and Frankfurt (+1): one hour more from 1:00 UTC on the last Sunday of
    March to 1:00 UTC on the last Sunday of October (EU rule)."""
    m = MARKETS[MARKET]
    y = utc.year
    if m["dst"] == "us":
        start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 2:00 EST
        end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    else:
        start = datetime.datetime.combine(_last_sunday(y, 3), datetime.time(1, 0))
        end = datetime.datetime.combine(_last_sunday(y, 10), datetime.time(1, 0))
    naive = utc.replace(tzinfo=None)
    return m["std"] + (1 if start <= naive < end else 0)


def to_eastern(utc):
    """UTC -> the current market's local time (New York by default)."""
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    """The current market's local time -> UTC."""
    std = MARKETS[MARKET]["std"]
    for off in (std + 1, std):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local - datetime.timedelta(hours=std)


use_market("us")
# ---------------------------------------------------------------- end of markets


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise AlertError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NYSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = HOLIDAYS.get(d.isoformat())
    if name:
        return False, MARKETS[MARKET]["exchange"] + " holiday: " + name
    return True, ""


def session(d):
    """(open_time, close_time) in New York time, or None if closed all day."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in EARLY_CLOSES else CLOSE)


def market_status(now_utc):
    local = to_eastern(now_utc)
    d = local.date()
    ok, reason = trading_day(d)
    st = {"market": MARKET, "now_new_york": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(),
          "weekday": local.strftime("%A"), "calendar_known": d.year in CALENDAR_YEARS,
          "open": False, "reason": reason, "early_close": EARLY_CLOSES.get(d.isoformat())}
    s = session(d)
    if s:
        o, c = s
        st["opens"], st["closes"] = o.strftime("%H:%M"), c.strftime("%H:%M")
        if o <= local.time() < c:
            st["open"] = True
            st["reason"] = "regular session"
        elif local.time() < o:
            st["reason"] = "before the open"
        else:
            st["reason"] = "after the close"
    # next open
    nd = d if (s and local.time() < s[0]) else d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd):
            st["next_open"] = "%s %s" % (nd.isoformat(), OPEN.strftime("%H:%M"))
            break
        nd += datetime.timedelta(days=1)
    return st


# ---------------------------------------------------------------- quotes

def stooq_url(sym):
    if market_of(sym) != "us":
        raise AlertError("the stooq source covers New York tickers here: use --source yahoo for %s" % sym)
    return "https://stooq.com/q/l/?s=%s.us&f=sd2t2ohlcv&h&e=csv" % sym.lower().replace(".", "-")


def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    ysym = sym.upper() if market_of(sym) != "us" else sym.upper().replace(".", "-")
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, ysym)


def parse_stooq(text, sym):
    """Symbol,Date,Time,Open,High,Low,Close,Volume -> quote. N/D means no data."""
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows:
        raise AlertError("Stooq returned no rows for %s" % sym)
    r = {k.strip().lower(): (v or "").strip() for k, v in rows[0].items() if k}
    close = r.get("close", "")
    if not close or close.upper() == "N/D":
        raise AlertError("Stooq has no data for %s (check the ticker)" % sym)

    def f(k):
        v = r.get(k, "")
        try:
            return float(v)
        except ValueError:
            return None
    return {"symbol": sym.upper(), "price": float(close), "open": f("open"), "high": f("high"),
            "low": f("low"), "prev_close": None, "time": ("%s %s" % (r.get("date", ""), r.get("time", ""))).strip(),
            "source": "stooq"}


def parse_yahoo(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = m.get("regularMarketTime")
    when = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t else "")
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": sym.upper(), "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "currency": m.get("currency"), "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch(url, timeout=20):
    code, text = _get(url, timeout)
    if code != 200:
        raise AlertError("%s answered HTTP %d" % (url.split("?")[0], code))
    return text


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, source, sym):
    ext = "csv" if source == "stooq" else "json"
    base = os.path.join(folder, "%s-%s.%s" % (source, sym, ext))
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline quote file %s" % base)


def get_quotes(symbols, source="yahoo", offline=None, pause=PAUSE):
    """{SYM: quote} and [errors]. One request per ticker, with a pause between."""
    quotes, errors = {}, []
    for i, sym in enumerate(symbols):
        sym = sym.upper()
        try:
            if offline:
                text = _offline_text(offline, source, sym)
            else:
                if i and pause:
                    _sleep(pause)
                text = fetch(stooq_url(sym)) if source == "stooq" else fetch_yahoo(sym)
            quotes[sym] = parse_stooq(text, sym) if source == "stooq" else parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


FX_PAIRS = {"london": "GBPUSD", "frankfurt": "EURUSD"}


def get_fx(symbols, offline=None):
    """The dollar rates the London and Frankfurt tickers need: {"GBPUSD": {"rate", "time"}}.
    Yahoo's GBPUSD=X and EURUSD=X, free and delayed. Offline: yahoo-GBPUSD=X.json in the folder."""
    out = {}
    for mk in sorted({market_of(s) for s in symbols} - {"us"}):
        pair = FX_PAIRS[mk]
        try:
            if offline:
                text = _offline_text(offline, "yahoo", pair + "=X")
            else:
                text = fetch_yahoo(pair + "=X")
            q = parse_yahoo(text, pair + "=X")
            out[pair] = {"rate": q["price"], "time": q.get("time")}
        except AlertError as e:
            out[pair] = {"rate": None, "error": str(e)}
    return out


def write_prices(path, quotes, now_utc, source, fx=None):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "note": ("Delayed quotes for alerts and sizing checks. Your broker's price is the one that fills. "
                    "detail.fetched is when each price was read. London prices are in pence, Frankfurt in "
                    "euros; fx holds dollars per pound or euro."),
           "prices": prices, "detail": detail}
    rates = dict(old.get("fx", {}))
    rate_detail = dict(old.get("fx_detail", {}))
    for pair, f in (fx or {}).items():
        if f.get("rate"):
            rates[pair] = f["rate"]
            rate_detail[pair] = {"time": f.get("time"), "fetched": stamp}
    if rates:
        doc["fx"], doc["fx_detail"] = rates, rate_detail
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as a New York time like \"15:55\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that New York date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this New York date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_eastern(utc).date().isoformat() == today


def ring_time(rule, status):
    """The time a time rule rings today, moved earlier on an early close if "before_close"
    (3 hours in New York, 4 in London: 16:30 to 12:30)."""
    at = _hhmm(rule.get("at"))
    if rule.get("before_close") and status.get("early_close"):
        at = (datetime.datetime.combine(datetime.date(2000, 1, 1), at)
              - datetime.timedelta(minutes=CLOSE_MIN - EARLY_MIN)).time()
    return at


def rule_market(rule):
    """A rule's market: its "market", else its ticker's suffix (VOD.L is London), else New York."""
    try:
        return market_key(rule.get("market")) if rule.get("market") else market_of(rule.get("symbol"))
    except ValueError:
        raise AlertError("rule %s: unknown market %r (us, london or frankfurt)" % (rule.get("id"), rule.get("market")))


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    us_status = status
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        # each rule runs on its own market's clock and calendar: a VOD.L stop on London time
        use_market(rule_market(r))
        status = us_status if MARKET == "us" else market_status(now_utc)
        today = status["date"]
        local = to_eastern(now_utc)
        before = len(rings)
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": localize(
                    "Opening bell: the NYSE is open (%s New York)%s."
                    % (status["now_new_york"], (", early close at %s today, " % (
                        "1:00 p.m." if MARKET == "us" else ampm_of((EARLY.hour, EARLY.minute)))
                        + status["early_close"]) if status.get("early_close") else ""))})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = ring_time(r, status)
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s New York (set for %s)" % (label[:1].upper(), label[1:],
                                                                 local.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    ", the market has closed."
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    msg += (" If %s is still open, close it: trader-broker flatten --symbol %s builds the sell "
                            "ticket, then trader-risk and your yes." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": localize(msg)})
                rung[rid] = stamp
            continue
        sym = r["symbol"].upper()
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s $%.2f: %s your %s $%s%s."
                              % (sym, "is" if status["open"] else "last", p, word, label, _n(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to $%.2f (from $%.2f)."
                              % (sym, move, p, float(ref))})
                rung[rid] = stamp
        for ring in rings[before:]:
            ring["message"] = localize(ring["message"])   # p or euros for a London or Frankfurt price
    use_market("us")
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by-close time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = str(plan["symbol"]).upper()
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    mk = market_of(sym)
    flat = plan.get("flat_by") or (cfg_flat_by if mk == "us" else None) or market_times({}, mk)["flat_by"]
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 15:55" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat,
                "label": "flat by close", "before_close": True})
    for r in out:
        if mk != "us":
            r["market"] = mk
        if plan.get("date"):
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(str(p.get("ticker") or p.get("symbol") or "").upper())
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]).upper())
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    try:
        use_market(a.market)
    except ValueError as e:
        raise AlertError(str(e))
    st = market_status(parse_now(a.now))
    if a.json:
        say(json.dumps(st, indent=2))
    else:
        say("New York %s (%s): NYSE %s, %s." % (st["now_new_york"], st["weekday"],
                                                  "OPEN" if st["open"] else "closed", st["reason"]))
        if st.get("opens"):
            say("Today's session: %s to %s%s." % (st["opens"], st["closes"],
                                                    (" (early close: %s)" % st["early_close"]) if st.get("early_close") else ""))
        if not st["open"] and st.get("next_open"):
            say("Next open: %s New York." % st["next_open"])
        if not st["calendar_known"]:
            say("Holiday list covers %s only: check the exchange's own calendar for this date."
                % " and ".join(map(str, CALENDAR_YEARS)))
    return 0 if st["open"] else 1


def cmd_quote(a):
    syms = _dedupe([s.upper() for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which tickers? alerts.py quote TSLA KO, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.source, a.offline)
    fx = get_fx(syms, a.offline) if quotes else {}
    if a.write and quotes:
        write_prices(a.write, quotes, now, a.source, fx)
    if a.json:
        say(json.dumps({"quotes": quotes, "fx": fx, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close $%.2f)" % (100.0 * (q["price"] / q["prev_close"] - 1), q["prev_close"])
                use_market(market_of(s))   # London in pence, Frankfurt in euros
                say("%-6s $%.2f%s  %s  [%s]" % (s, q["price"], chg, q.get("time", ""), q["source"]))
                use_market("us")
        for pair, f in sorted(fx.items()):
            if f.get("rate"):
                say("%s %.4f: dollars per %s, for sizing in dollars (trader-budget, trader-risk)."
                    % (pair, f["rate"], "pound" if pair.startswith("GBP") else "euro"))
            else:
                say("error: no %s rate: %s" % (pair, f.get("error")))
        for e in errors:
            say("error: " + e)
        if a.write and quotes:
            say("Wrote %d price(s) to %s." % (len(quotes), a.write))
        say("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    status = market_status(now)
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([str(r.get("symbol", "")).upper() for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, a.source, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now, a.source)
    if a.json:
        say(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        for mk in _dedupe([rule_market(r) for r in rules]) or ["us"]:
            use_market(mk)
            st = status if mk == "us" else market_status(now)
            say("NYSE %s (%s New York, %s)." % ("OPEN" if st["open"] else "closed", st["now_new_york"],
                                                st["reason"]))
        use_market("us")
        for r in rings:
            say("ALERT " + r["message"])
        if not rings:
            say("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s $%.2f" % (s, quotes[s]["price"]) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            say("error: " + e)
        if rings:
            say("An alert is information, not an order. A bracket's stop-loss and take-profit already sit at "
                  "the broker: check get_open_position and record any fill (ticket.py fill). To close a "
                  "position yourself: ticket.py flatten, then trader-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            say("Added %s: %s" % (r["id"], json.dumps(r)))
        say("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free quotes are delayed; the broker's bracket orders are what actually stop you out.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
        if a.market and market_key(a.market) != "us":
            rule["market"] = market_key(a.market)
            rule["id"] = a.id or "opening-bell-%s" % rule["market"]
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants a New York time like 15:55")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = a.symbol.upper()
        if a.before_close:
            rule["before_close"] = True
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = a.symbol.upper()
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    say("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        say("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        say("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        say("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    say("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NYSE clock (trader-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--source", default="yahoo", choices=["yahoo", "stooq"])
        p.add_argument("--offline", metavar="DIR", help="read <source>-<TICKER>.json|csv files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = New York time)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NYSE (or London, Frankfurt) open?")
    p.add_argument("--market", default="us", help="us (default), london or frankfurt")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add tickers from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true")
    p.add_argument("--market", help="the opening bell of us (default), london or frankfurt")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, New York time")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by-close from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by close\"")
    p.add_argument("--before-close", action="store_true",
                   help="a time alert that moves 3 hours earlier on a 1:00 p.m. early close")
    p.add_argument("--date", help="watch this rule on this New York date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        say("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
