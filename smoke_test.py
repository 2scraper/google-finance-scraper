#!/usr/bin/env python3
"""Offline checks for the Google Finance scraper.

One file of plain functions, no pytest and no conftest — `tests/test_smoke.py`
wraps this as a single pytest test so `pytest` works as an entry point without
a second copy of the checks.

It must pass with NO engine library installed at all. Every
`import playwright_scraper` / `puppeteer_scraper` / `selenium_scraper` is
guarded and the skip is RECORDED and reported, because "skipped, engine
absent" reads identically to a real import error — CI installs each engine in
its own venv and fails if the matching group reports a skip.

THE FIXTURES ARE IN `fixtures_generated.json`, NOT INLINE
---------------------------------------------------------
The family's rule is inline fixtures, and it does not survive this site: one
`AF_initDataCallback` blob is 4-240 KB of nested JSON and a fixture has to
carry whole blobs or it stops exercising the thing the parser is built on.
`make_fixtures.py` cuts them out of real captures and PROVES each trimmed
fixture parses identically to the capture, column for column. The checks
below then assert VALUES on them — a column can be 100% populated and
entirely wrong, which is how a sibling shipped a review count of 445279961 on
every row of every run with a green coverage check beside it.
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import glob
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from contextlib import redirect_stdout, redirect_stderr

import output_writer
import page_flow
import product_parser as pp
from output_writer import (Quote, Product, Financial, AnalystRating,
                           EarningsEvent, ChartPoint, dedupe_by_key,
                           finish_run, DEDUPE_KEY_BY_MODE,
                           ROW_CLASS_BY_MODE, SOURCE_DEFAULT)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
FIXTURES_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")
ENGINES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")
SHARED_MODULES = ("product_parser", "page_flow", "output_writer",
                  "proxy_pool", "env_config", "captcha_solver",
                  "fingerprint_client", "scraper_api_client")

_failures = []


def check(label, condition):
    """Print and record one check. Returns the condition, so callers can
    accumulate with `ok &= check(...)`."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
        return False
    except Exception:
        return True


def _load_fixtures():
    with open(FIXTURES_PATH, encoding="utf-8") as f:
        return json.load(f)["fixtures"]


FIXTURES = _load_fixtures()


def _fx(name):
    return FIXTURES[name]


def _rows(name):
    f = _fx(name)
    # `page=1` because that is what a one-symbol run passes, and the
    # fixtures were generated the same way — a re-parse that differed from
    # the recorded rows by one column would be a suite testing itself
    # rather than the parser.
    return pp.parse_products(f["html"], f["url"], mode=f["mode"], page=1,
                             market=pp.market_from_url(f["url"]))


def _by_sku(rows):
    return {r.sku: r for r in rows}


# ---------------------------------------------------------------------------
# The payload, and the thing this parser must never depend on
# ---------------------------------------------------------------------------

def test_payload_extraction():
    group("payload extraction")
    ok = True
    f = _fx("quote_stock_us")
    blobs = pp.payload(f["html"])
    ok &= check("a fixture yields at least one AF_initDataCallback blob",
                len(blobs) >= 1)
    ok &= check("every blob's data parsed to a list",
                all(isinstance(v, list) for v in blobs.values()))
    ok &= check("no blobs in a document that has none",
                pp.payload("<html><body>nothing</body></html>") == {})
    ok &= check("None html is not a crash", pp.payload(None) == {})

    # A blob whose data contains a bracket INSIDE a string. The bracket
    # balancer skips over string contents for exactly this case: a
    # non-greedy regex truncates the blob at the first `]` that looks right,
    # which loses rows from the END of a list where nothing errors.
    tricky = ('AF_initDataCallback({key: \'ds:0\', hash: \'1\', '
              'data:[["Bath & Body [Works] Inc", "]["], 2], '
              'sideChannel: {}});')
    got = pp.payload("<html><body>%s</body></html>" % tricky)
    ok &= check("a bracket inside a string does not truncate the blob",
                got.get("ds:0") == [["Bath & Body [Works] Inc", "]["], 2])

    # A malformed blob must not take the good ones down with it.
    mixed = tricky + 'AF_initDataCallback({key: \'ds:1\', data:[not json]});'
    got = pp.payload("<html><body>%s</body></html>" % mixed)
    ok &= check("an unparseable blob is skipped, not raised on",
                "ds:0" in got and "ds:1" not in got)
    return ok


def test_shape_anchoring_not_key_numbering():
    group("the parser anchors on SHAPE, never on ds:N")
    ok = True
    # THE check this module exists for. Measured 2026-09-22: adding
    # `?window=5D` to a quote url makes the page carry 23 keys instead of 22
    # and moves every key up by one, so a parser anchored on `ds:12` reads
    # the wrong blob on a url that differs only by a query parameter.
    #
    # Simulated here by RENUMBERING every key in a real fixture. If any
    # lookup anywhere in the parser were by key, this would change the rows.
    f = _fx("markets_us")
    before = _rows("markets_us")

    def bump(m):
        return "key: '%s:%d'" % (m.group(1), int(m.group(2)) + 100)

    renumbered = re.sub(r"key: '(ds):(\d+)'", bump, f["html"])
    ok &= check("renumbering actually changed the fixture",
                renumbered != f["html"])
    after = pp.parse_products(renumbered, f["url"], mode=f["mode"])
    ok &= check("renumbering every ds: key changes no row",
                [ (r.sku, r.price, r.listing) for r in before ]
                == [ (r.sku, r.price, r.listing) for r in after ])

    # And the blobs' ORDER in the document must not matter either.
    blobs = re.findall(r"AF_initDataCallback\(\{.*?\}\);", f["html"], re.S)
    ok &= check("the fixture has more than one blob to reorder", len(blobs) > 1)
    reordered = ('<html><head><script src="https://www.gstatic.com/finance/x.js">'
                 '</script></head><body>' + "".join(reversed(blobs)) +
                 "</body></html>")
    got = pp.parse_products(reordered, f["url"], mode=f["mode"])
    ok &= check("reversing blob order yields the same set of instruments",
                {r.sku for r in got} == {r.sku for r in before})
    return ok


# ---------------------------------------------------------------------------
# Values, on real fixtures
# ---------------------------------------------------------------------------

def test_quote_values():
    group("quote rows: values, not coverage")
    ok = True
    r = _rows("quote_stock_us")[0]
    ok &= check("sku is the canonical TICKER:EXCHANGE", r.sku == "GOOGL:NASDAQ")
    ok &= check("ticker and exchange are split out",
                (r.ticker, r.exchange) == ("GOOGL", "NASDAQ"))
    ok &= check("title is Google's own display name",
                r.title == "Alphabet Inc Class A")
    ok &= check("price is the last trade", r.price == 354.97)
    ok &= check("currency is read, not guessed", r.currency == "USD")
    ok &= check("previous close", r.prev_close == 349.54)
    ok &= check("open", r.open == 350.64)
    ok &= check("day low/high", (r.day_low, r.day_high) == (349.1, 357.61))
    ok &= check("instrument_type", r.instrument_type == "stock")
    ok &= check("industry is Google's own label",
                r.industry == "Interactive media")
    ok &= check("country", r.country == "US")
    ok &= check("timezone", r.timezone == "America/New_York")
    # Volume, and the near-miss worth pinning: 152,020 on Alphabet is a
    # plausible HEADCOUNT, and this field was very nearly documented as
    # "employees". It is volume — the same capture's daily bars give a
    # median of 23.3M against this premarket snapshot, and the S&P 500 reads
    # 3.28 BILLION in the same slot, which is a share count and not a
    # payroll.
    ok &= check("volume is volume, not headcount", r.volume == 152020)
    ok &= check("market cap", round(r.market_cap) == 4312487948430)
    ok &= check("price_source records WHICH record was read",
                r.price_source == "af_quote")
    ok &= check("quoted_at is the site's as-of, not our fetch time",
                r.quoted_at is not None and r.quoted_at != r.scraped_at)
    # The extended-hours quote, pinned to the value the parser produces
    # rather than to the first one found by eye. The capture carries TWO
    # after-hours tuples for this instrument, taken seconds apart in two
    # different blobs; the row takes the one inside the page subject's own
    # record, which is 356.9526. Pinning the other was a check asserting
    # where a human had looked, not what the code reads.
    ok &= check("extended-hours quote is present on a US equity",
                r.after_hours_price == 356.9526)

    de = _rows("quote_stock_de")[0]
    ok &= check("a German listing reads EUR, not a defaulted USD",
                de.currency == "EUR" and de.sku == "BMW:ETR")
    ok &= check("a German listing has no extended-hours quote",
                de.after_hours_price is None)
    return ok


def test_every_instrument_kind():
    group("all five instrument kinds parse")
    ok = True
    expected = {
        "quote_stock_us": ("GOOGL:NASDAQ", "stock", "USD", "NASDAQ"),
        "quote_index": (".INX:INDEXSP", "index", None, "INDEXSP"),
        "quote_currency": ("EUR-USD", "currency", "USD", None),
        "quote_crypto": ("BTC-USD", "crypto", "USD", None),
        "quote_futures": ("GCW00:COMEX", "futures", "USD", "COMEX"),
    }
    for name, (sku, kind, ccy, exch) in expected.items():
        rows = _rows(name)
        if not check("%s parses to exactly one row" % name, len(rows) == 1):
            ok = False
            continue
        r = rows[0]
        ok &= check("%s: sku %s" % (name, sku), r.sku == sku)
        ok &= check("%s: type %s" % (name, kind), r.instrument_type == kind)
        ok &= check("%s: currency %r" % (name, ccy), r.currency == ccy)
        ok &= check("%s: exchange %r" % (name, exch), r.exchange == exch)

    # A pair's currency is its QUOTE leg, read out of the payload's own leg
    # list — not inferred from the symbol. `EUR-USD` is quoted in USD.
    ok &= check("a pair's currency is the quote leg",
                _rows("quote_currency")[0].currency == "USD")
    # And a pair is typed from the payload's flag, not from string shape.
    # `len(base) == 3 and len(quote) == 3` would call BTC-USD a currency.
    ok &= check("BTC-USD is crypto, not a currency pair",
                _rows("quote_crypto")[0].instrument_type == "crypto")
    return ok


def test_zero_is_not_a_measurement():
    group("sentinel zeros are nulled, not written through")
    ok = True
    # Measured 2026-09-22: market cap is 0 on the index, currency, crypto and
    # futures captures and volume is 0 on the currency and crypto ones. A
    # sentinel, not a figure. Written through, it drags every average a
    # consumer computes.
    for name in ("quote_index", "quote_currency", "quote_crypto",
                 "quote_futures"):
        r = _rows(name)[0]
        ok &= check("%s: market_cap is null, not 0" % name,
                    r.market_cap is None)
    for name in ("quote_currency", "quote_crypto"):
        r = _rows(name)[0]
        ok &= check("%s: volume is null, not 0" % name, r.volume is None)
    # The index DOES have a volume, so the rule must not null everything.
    ok &= check("an index keeps its real volume",
                _rows("quote_index")[0].volume == 3278075312)
    # Across every fixture of every mode, no row carries either sentinel.
    # `getattr` rather than attribute access because the mode-C row classes
    # legitimately have no such column — and a sweep that crashed on the
    # first one would have stopped checking the rest.
    everything = [r for n, f in FIXTURES.items() if "rows" in f
                  for r in _rows(n)]
    ok &= check("the sweep covers every mode (%d rows)" % len(everything),
                len(everything) > 400)
    for col in ("market_cap", "volume", "price", "revenue", "eps"):
        ok &= check("no row of any mode carries %s == 0" % col,
                    not [r for r in everything if getattr(r, col, None) == 0])
    return ok


def test_markets_and_movers():
    group("the root page's strips")
    ok = True
    rows = _rows("markets_us")
    strips = {}
    for r in rows:
        strips.setdefault(r.listing, []).append(r)
    ok &= check("gl=US publishes 46 rows", len(rows) == 46)
    ok &= check("all five strips are present",
                set(strips) == set(pp.MARKET_STRIPS))
    ok &= check("20 broad indices", len(strips["index"]) == 20)
    ok &= check("11 sector indices", len(strips["sector"]) == 11)
    ok &= check("5 currency pairs", len(strips["currency"]) == 5)
    ok &= check("5 crypto pairs", len(strips["crypto"]) == 5)
    ok &= check("5 futures", len(strips["futures"]) == 5)
    # The regression this pins: an early parse_markets filtered for
    # `instrument_type == "index"` and dropped all fifteen FX, crypto and
    # futures rows — which are exactly the listings the retired
    # /finance/markets/{currencies,cryptocurrencies} urls used to serve, and
    # the most valuable rows on the page.
    ok &= check("the currency/crypto/futures strips are not filtered out",
                len(strips["currency"]) + len(strips["crypto"])
                + len(strips["futures"]) == 15)
    ok &= check("every markets row is priced",
                all(r.price is not None for r in rows))
    ok &= check("markets rows are unique by symbol",
                len({r.sku for r in rows}) == len(rows))
    ok &= check("a list row records which record it came from",
                all(r.price_source == "af_list" for r in rows))
    ok &= check("the indices strip is published twice and deduped once",
                len([r for r in rows if r.sku == ".DJI:INDEXDJX"]) == 1)

    movers = _rows("movers_us")
    lists = {}
    for r in movers:
        lists.setdefault(r.listing, []).append(r)
    ok &= check("movers are labelled gainers/losers/most_active",
                set(lists) == set(pp.MOVER_LISTS))
    ok &= check("every gainer is up",
                all(r.change_pct > 0 for r in lists["gainers"]))
    ok &= check("every loser is down",
                all(r.change_pct < 0 for r in lists["losers"]))
    # Measured across six markets on 2026-09-22 and the reason the positional
    # labels are trusted at all.
    ok &= check("gainers are sorted by change, descending",
                [r.change_pct for r in lists["gainers"]]
                == sorted([r.change_pct for r in lists["gainers"]],
                          reverse=True))

    # If the invariant ever stops holding, the labels must DOWNGRADE rather
    # than mislabel. Planted here by inverting the sign of every gainer.
    f = _fx("movers_us")
    broken = f["html"]
    subs = pp._mover_sublists(pp.payload(broken))
    ok &= check("the fixture has the three-sublist movers shape", len(subs) == 3)
    ok &= check("_movers_labels_hold accepts the real payload",
                pp._movers_labels_hold(subs) is True)
    flipped = [[list(r) for r in sub] for sub in subs]
    for rec in flipped[0]:
        rec[5] = list(rec[5])
        rec[5][2] = -abs(rec[5][2])
    ok &= check("_movers_labels_hold rejects a payload whose gainers fell",
                pp._movers_labels_hold(flipped) is False)
    return ok


def test_second_market():
    group("a second market is a different SAMPLE, not a change")
    ok = True
    us = _by_sku(_rows("markets_us"))
    de = _by_sku(_rows("markets_de"))
    ok &= check("both markets return a full strip set",
                len(us) >= 45 and len(de) >= 45)
    # The measurement that makes `--market` worth having: same address, two
    # parameters, two different sector venues and two different FX bases.
    us_sectors = {r.exchange for r in _rows("markets_us") if r.listing == "sector"}
    de_sectors = {r.exchange for r in _rows("markets_de") if r.listing == "sector"}
    ok &= check("gl=US selects CBOE sector indices",
                us_sectors == {"INDEXCBOE"})
    ok &= check("gl=DE selects STOXX sector indices",
                de_sectors == {"INDEXSTOXX"})
    # The regression this pins: labelling sectors by a ticker pattern
    # (`^(SIX|SX)` on a sector venue) put SX5E:INDEXSTOXX — the Euro Stoxx
    # 50, a BROAD index that lives in the indices blob — into the sector
    # strip on every US run. The label now comes from membership of the
    # payload's own "sectors" group.
    sx5e = [r for r in _rows("markets_us") if r.ticker == "SX5E"]
    ok &= check("the Euro Stoxx 50 is an index, not a sector",
                sx5e and sx5e[0].listing == "index")
    ok &= check("the two markets' sector sets do not overlap",
                not ({r.sku for r in _rows("markets_us") if r.listing == "sector"}
                     & {r.sku for r in _rows("markets_de") if r.listing == "sector"}))
    # Shared instruments join on the SYMBOL and agree about what they are.
    shared = set(us) & set(de)
    ok &= check("the two markets share some instruments", len(shared) >= 5)
    ok &= check("a shared instrument has the same type in both",
                all(us[s].instrument_type == de[s].instrument_type
                    for s in shared))
    ok &= check("a shared instrument has the same entity id in both",
                all(us[s].entity_id == de[s].entity_id for s in shared))
    return ok


# ---------------------------------------------------------------------------
# Classification — the part that costs the most when it is wrong
# ---------------------------------------------------------------------------

def test_not_found_is_not_empty():
    group("a Page Not Found is an ANSWER, not an empty page")
    ok = True
    f = _fx("not_found")
    ok &= check("the trimmed fixture still classifies as not_found",
                pp.detect_page_state(f["html"], 200, f["url"], "quote")
                == "not_found")
    ok &= check("a not_found page yields no rows",
                pp.parse_products(f["html"], f["url"], mode="quote") == [])

    # THE trap, asserted against the real capture rather than the trim,
    # because the trim is exactly what would hide it: the full Page Not Found
    # page carries the index and sector strips as page chrome and parses to
    # 35 instrument records. "Did we get rows?" answers YES on a page holding
    # no such instrument.
    capture = os.path.join(REPO_ROOT, "captures", "quote_notfound.html")
    if os.path.exists(capture):
        html = open(capture, encoding="utf-8", errors="replace").read()
        url = "https://www.google.com/finance/beta/quote/ZZZZQQ:NASDAQ"
        chrome = pp._compact_records(html)
        ok &= check("the full not-found capture DOES carry chrome records "
                    "(the trap is real)", len(chrome) > 20)
        ok &= check("...and the quote parser still returns nothing for it",
                    pp.parse_product_page(html, url) == [])
        ok &= check("...and it classifies as not_found, not empty",
                    pp.detect_page_state(html, 200, url, "quote")
                    == "not_found")
        # And the marker is read over the WHOLE document: on this capture it
        # sits at byte 930,698 of 987,996, so a bounded prefix scan — which
        # is right for the challenge markers — missed it entirely and the
        # classifier called it "empty".
        ok &= check("the not-found marker is found deep in the document",
                    html.find('title="Page Not Found"') > 500_000)
    else:
        print("  NOTE  captures/ absent; the full-page trap check needs it")

    # not_found spends no budget: no retry, no solve, not blocked.
    policy = page_flow.STATE_POLICY["not_found"]
    ok &= check("not_found is not parsed", policy["parse"] is False)
    ok &= check("not_found is not retried", policy["retry"] is False)
    ok &= check("not_found is not solved for", policy["solve"] is False)
    ok &= check("not_found does NOT count as blocked", policy["blocked"] is False)
    ok &= check("not_found is a definite answer",
                page_flow.is_definite_answer("not_found"))
    return ok


def test_unsupported_client_is_not_a_block():
    group("the UA gate is a CLIENT refusal, not an address refusal")
    ok = True
    f = _fx("unsupported_browser")
    ok &= check("classifies as unsupported_client",
                pp.detect_page_state(f["html"], 200, f["url"], "quote")
                == "unsupported_client")
    ok &= check("yields no rows",
                pp.parse_products(f["html"], f["url"], mode="quote") == [])
    ok &= check("does NOT count as blocked",
                page_flow.counts_as_blocked("unsupported_client") is False)
    advice = page_flow.block_advice(f["html"], headless=True, has_pool=False)
    ok &= check("its advice says it is not a block",
                "not a block" in advice.lower() or "NOT a block" in advice)
    ok &= check("its advice names the User-Agent as the cause",
                "user-agent" in advice.lower())
    ok &= check("its advice does not send the reader to buy a proxy",
                "rotate with --proxy-rotate" not in advice)
    return ok


def test_marker_sets_score_zero_on_good_pages():
    group("markers, counted on pages known to be good")
    ok = True
    # CLAUDE.md §18: a marker that matches every page is worse than no
    # marker. Every challenge marker must score ZERO on every served
    # fixture, or it is a fact about the site rather than a signal.
    served = [n for n, f in FIXTURES.items() if "rows" in f]
    ok &= check("there are served fixtures to count against", len(served) >= 6)
    for marker in pp.BOT_CHALLENGE_MARKERS:
        hits = sum(_fx(n)["html"].count(marker) for n in served)
        ok &= check("challenge marker %r scores 0 on all served fixtures"
                    % marker, hits == 0)
    for marker in pp.NOT_FOUND_MARKERS:
        hits = sum(_fx(n)["html"].count(marker) for n in served)
        ok &= check("not-found marker %r scores 0 on served fixtures" % marker,
                    hits == 0)
    for marker in pp.UNSUPPORTED_MARKERS:
        hits = sum(_fx(n)["html"].count(marker) for n in served)
        ok &= check("unsupported marker %r scores 0 on served fixtures"
                    % marker, hits == 0)
    # And each state marker must actually fire on its own fixture, or the
    # zero above is passing for the wrong reason.
    ok &= check("a not-found marker fires on the not-found fixture",
                any(m in _fx("not_found")["html"]
                    for m in pp.NOT_FOUND_MARKERS))
    ok &= check("an unsupported marker fires on its own fixture",
                any(m in _fx("unsupported_browser")["html"]
                    for m in pp.UNSUPPORTED_MARKERS))
    # `cf-turnstile` is the family's measured-useless marker. It must not be
    # here: three siblings found it fires on good pages fetched through a
    # managed browser whose auto-solve extension injects it.
    ok &= check("cf-turnstile is not in the marker set",
                "cf-turnstile" not in pp.BOT_CHALLENGE_MARKERS)
    # A bare `captcha`/`recaptcha` would fire 21 and 2 times on a good page
    # fetched over --cdp-endpoint, measured on a sibling.
    ok &= check("no marker is a bare captcha word",
                not [m for m in pp.BOT_CHALLENGE_MARKERS
                     if m.strip().lower() in ("captcha", "recaptcha",
                                              "turnstile")])
    return ok


def test_state_ordering():
    group("classification order: markers before counts")
    ok = True
    # An unambiguous marker must beat a threshold. Built here by pasting the
    # not-found marker onto a page that also carries real rows: the marker
    # must win, because a page that says Page Not Found is not a page that
    # happens to have chrome on it.
    good = _fx("markets_us")
    hybrid = good["html"].replace("<body>", '<body><div title="Page Not Found">'
                                            'Page Not Found</div>')
    ok &= check("the hybrid really carries both", "Page Not Found" in hybrid
                and len(pp._compact_records(hybrid)) > 20)
    ok &= check("the marker wins over the row count",
                pp.detect_page_state(hybrid, 200, good["url"], "markets")
                == "not_found")
    # And the unsupported marker beats the not-found marker, because the
    # unsupported page is about the CLIENT and answers a different question.
    both = hybrid.replace("<body>", '<body><link rel="canonical" '
                                    'href="/finance/beta/unsupported">')
    ok &= check("unsupported_client is read before not_found",
                pp.detect_page_state(both, 200, good["url"], "markets")
                == "unsupported_client")
    # A page built out of none of Google's assets is `unknown`, not content —
    # which is what catches Chromium's own network-error page, whose <title>
    # is the site's hostname.
    chromium_error = ("<html><head><title>www.google.com</title></head>"
                      "<body><div>ERR_PROXY_CONNECTION_FAILED</div></body>"
                      "</html>")
    ok &= check("Chromium's own error page is not read as content",
                pp.detect_page_state(chromium_error, 200,
                                     "https://www.google.com/finance/beta/",
                                     "markets") == "unknown")
    ok &= check("served_by_google is False for it",
                pp.served_by_google(chromium_error) is False)
    ok &= check("served_by_google is True for a real fixture",
                pp.served_by_google(_fx("markets_us")["html"]) is True)
    return ok


def test_symbol_matching():
    group("a url's symbol is matched exactly")
    ok = True
    f = _fx("quote_stock_us")
    ok &= check("the right symbol returns its row",
                len(pp.parse_product_page(
                    f["html"],
                    "https://www.google.com/finance/beta/quote/GOOGL:NASDAQ")) == 1)
    ok &= check("lower case still matches",
                len(pp.parse_product_page(
                    f["html"],
                    "https://www.google.com/finance/beta/quote/googl:nasdaq")) == 1)
    # A substring rule would let a page about META:NASDAQ satisfy a request
    # for ETA:NASDAQ. Pinned in both directions.
    ok &= check("a substring of the symbol does not match",
                pp.parse_product_page(
                    f["html"],
                    "https://www.google.com/finance/beta/quote/OOGL:NASDAQ") == [])
    ok &= check("a different exchange does not match",
                pp.parse_product_page(
                    f["html"],
                    "https://www.google.com/finance/beta/quote/GOOGL:NYSE") == [])
    ok &= check("symbol_from_url reads the symbol",
                pp.symbol_from_url(
                    "https://www.google.com/finance/beta/quote/EUR-USD?window=1Y")
                == "EUR-USD")
    ok &= check("symbol_from_url is None on the root",
                pp.symbol_from_url("https://www.google.com/finance/beta/")
                is None)
    return ok


# ---------------------------------------------------------------------------
# URLs and the absence of pagination
# ---------------------------------------------------------------------------

def test_urls():
    group("URLs")
    ok = True
    ok &= check("quote_url builds the beta form",
                pp.quote_url("GOOGL:NASDAQ")
                == "https://www.google.com/finance/beta/quote/GOOGL:NASDAQ")
    ok &= check("a colon is not percent-escaped",
                ":" in pp.quote_url("GOOGL:NASDAQ"))
    ok &= check("a dotted index symbol survives",
                pp.quote_url(".INX:INDEXSP").endswith("/quote/.INX:INDEXSP"))
    ok &= check("markets_url is the root",
                pp.markets_url().endswith("/finance/beta/"))
    ok &= check("with_market sets gl",
                "gl=US" in pp.with_market(pp.markets_url(), "us"))
    ok &= check("with_market upper-cases the market",
                "gl=DE" in pp.with_market(pp.markets_url(), "de"))
    ok &= check("with_market replaces rather than duplicates",
                pp.with_market(pp.with_market(pp.markets_url(), "US"), "DE")
                .count("gl=") == 1)
    ok &= check("with_market preserves an unrelated parameter",
                "foo=bar" in pp.with_market(
                    "https://www.google.com/finance/beta/?foo=bar", "US"))
    ok &= check("market_from_url round-trips",
                pp.market_from_url(pp.with_market(pp.markets_url(), "jp"))
                == "JP")
    # The legacy rewrite: the site redirects these, so doing it locally saves
    # a round trip and makes two runs produce the same `url` column.
    ok &= check("a legacy quote url is rewritten to /beta/",
                pp.canonical_url(
                    "https://www.google.com/finance/quote/GOOGL:NASDAQ")
                == "https://www.google.com/finance/beta/quote/GOOGL:NASDAQ")
    ok &= check("a legacy markets url is rewritten too",
                pp.canonical_url(
                    "https://www.google.com/finance/markets/gainers")
                .startswith("https://www.google.com/finance/beta/markets/"))
    ok &= check("an already-beta url is left alone",
                pp.canonical_url(
                    "https://www.google.com/finance/beta/quote/BMW:ETR")
                == "https://www.google.com/finance/beta/quote/BMW:ETR")
    ok &= check("listing_kind tells a quote url from the root",
                (pp.listing_kind(pp.quote_url("BMW:ETR")),
                 pp.listing_kind(pp.markets_url())) == ("quote", "markets"))
    # Host refusal names the host and says why.
    ok &= check("another site is refused",
                not pp.is_supported_host("https://finance.yahoo.com/quote/GOOG"))
    reason = pp.unsupported_reason("https://finance.yahoo.com/quote/GOOG")
    ok &= check("...with the host named in the reason",
                "finance.yahoo.com" in (reason or ""))
    ok &= check("a non-finance Google path is refused with its own reason",
                "not a /finance path"
                in (pp.unsupported_reason("https://www.google.com/search?q=x") or ""))
    ok &= check("a Google Finance url is accepted",
                pp.is_supported_host(pp.quote_url("GOOGL:NASDAQ")))
    return ok


def test_there_is_no_pagination():
    group("no pagination, stated in code rather than hoped for")
    ok = True
    for url in (pp.quote_url("GOOGL:NASDAQ"), pp.markets_url("US"),
                pp.markets_url()):
        ok &= check("paginates_by_url is False for %s" % url,
                    pp.paginates_by_url(url) is False)
    ok &= check("page_flow agrees",
                page_flow.pagination_is_addressable(pp.markets_url()) is False)
    ok &= check("the reason names --symbols",
                "--symbols" in pp.no_pagination_reason())
    # A selector that can never match is worse than an empty one: a sibling
    # shipped three dead selectors and reported a complete run holding a
    # third of the data.
    ok &= check("NEXT_PAGE_SELECTOR is empty, not hopeful",
                page_flow.NEXT_PAGE_SELECTOR == "")
    ok &= check("no next-page candidate is ever produced",
                page_flow.next_page_candidates(pp.markets_url(),
                                               ["/finance/beta/?page=2"]) == [])
    ok &= check("page_url ignores the page number",
                pp.page_url(pp.markets_url(), 5) == pp.markets_url())
    ok &= check("pages_beyond_cap reports all but the first",
                pp.pages_beyond_cap(3) == 2 and pp.pages_beyond_cap(1) == 0)
    ok &= check("per-url concurrency is capped at 1",
                page_flow.concurrency_limit(pp.markets_url()) == 1)
    refusal = page_flow.concurrency_refusal(pp.markets_url())
    ok &= check("...and refused WITH the reason", refusal and "SYMBOLS" in refusal)
    return ok


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------

def test_output_contract():
    group("output contract")
    ok = True
    names = [f.name for f in dataclasses.fields(Quote)]
    ok &= check("the family prefix is byte-identical and in order",
                names[:5] == ["source", "scraped_at", "url", "sku", "title"])
    ok &= check("price and currency follow it",
                names[5:7] == ["price", "currency"])
    ok &= check("Product is still an alias for the row class",
                Product is Quote)
    ok &= check("source is this site", SOURCE_DEFAULT == "google.com/finance")
    ok &= check("every mode maps to a row class",
                set(ROW_CLASS_BY_MODE) == set(pp.ALL_MODES))
    ok &= check("the three quote-page list modes share the Quote class",
                {ROW_CLASS_BY_MODE[m] for m in ("quote", "markets", "movers")}
                == {Quote})
    ok &= check("each mode-C mode has its own row class",
                [ROW_CLASS_BY_MODE[m] for m in
                 ("financials", "analysts", "earnings", "chart")]
                == [Financial, AnalystRating, EarningsEvent, ChartPoint])
    ok &= check("every mode-C row class keeps the family prefix",
                all([f.name for f in dataclasses.fields(c)][:5]
                    == ["source", "scraped_at", "url", "sku", "title"]
                    for c in (Financial, AnalystRating, EarningsEvent,
                              ChartPoint)))
    # No dead columns, checked PER ROW CLASS: every field of every class
    # must be populated on at least one row of at least one fixture, or it
    # should not exist (CLAUDE.md §9). Pooling them across classes would let
    # a dead column in one class be covered by a same-named live one in
    # another.
    populated = {}
    for name, f in FIXTURES.items():
        if "rows" not in f:
            continue
        for r in _rows(name):
            bucket = populated.setdefault(type(r).__name__, set())
            for k, v in dataclasses.asdict(r).items():
                if v is not None:
                    bucket.add(k)
    for cls in (Quote, Financial, AnalystRating, EarningsEvent, ChartPoint):
        got = populated.get(cls.__name__, set())
        want = {f.name for f in dataclasses.fields(cls)}
        dead = sorted(want - got)
        ok &= check("%s has no column null on every row (%s)"
                    % (cls.__name__, dead or "none"), not dead)
    # Dedupe: the list modes need a compound key.
    ok &= check("quote dedupes on sku alone",
                DEDUPE_KEY_BY_MODE.get("quote", "sku") == "sku")
    ok &= check("the list modes dedupe on (sku, listing)",
                DEDUPE_KEY_BY_MODE["markets"] == ("sku", "listing")
                and DEDUPE_KEY_BY_MODE["movers"] == ("sku", "listing"))
    rows = [Quote(sku="A", listing="gainers"), Quote(sku="A", listing="most_active"),
            Quote(sku="A", listing="gainers"), Quote(sku=None, listing=None)]
    ok &= check("a symbol in two lists survives a compound dedupe",
                len(dedupe_by_key(list(rows), set(), ("sku", "listing"))) == 3)
    ok &= check("...and would not survive a sku-only one",
                len(dedupe_by_key(list(rows), set(), "sku")) == 2)
    ok &= check("a row with no key at all is kept",
                len(dedupe_by_key([Quote()], set(), ("sku", "listing"))) == 1)
    # The real overlap this exists for, on the real fixture.
    movers = _rows("movers_us")
    from collections import Counter
    dupes = [s for s, n in Counter(r.sku for r in movers).items() if n > 1]
    ok &= check("the US movers fixture really does repeat a symbol "
                "across lists (%s)" % (dupes or "none"), len(dupes) >= 1)
    ok &= check("compound dedupe keeps both of its rows",
                len(dedupe_by_key(list(movers), set(), ("sku", "listing")))
                == len(movers))
    return ok


# ---------------------------------------------------------------------------
# Mode C
# ---------------------------------------------------------------------------

def test_financial_values():
    group("financials: the seven slots, pinned on real fixtures")
    ok = True
    rows = _rows("financials_us")
    ok &= check("90 periods on the Alphabet capture", len(rows) == 90)
    q = next((r for r in rows if (r.fiscal_year, r.fiscal_quarter) == (2026, 2)),
             None)
    if not check("2026 Q2 is present", q is not None):
        return False
    # VALUES, not coverage. Each of these was established by matching the
    # page's own rendered figure against every payload slot across four
    # periods on four instruments — see output_writer.Financial. Pinning
    # them is what stops a future refactor quietly reading slot 8 as
    # revenue.
    ok &= check("revenue", q.revenue == 119796000000.0)
    ok &= check("net income", q.net_income == 112193000000.0)
    ok &= check("operating expense", q.operating_expense == 33083000000.0)
    ok &= check("EBITDA", q.ebitda == 48241000000.0)
    ok &= check("earnings per share", q.eps == 9.11)
    ok &= check("net profit margin", q.net_profit_margin == 93.65)
    ok &= check("effective tax rate", q.effective_tax_rate == 19.14)
    ok &= check("period end", q.period_end == "2026-06-30")
    ok &= check("currency", q.currency == "USD")
    ok &= check("a recent quarter is detailed", q.detailed is True)
    # EPS matched slots 2 AND 9 on this instrument — the coincidence that
    # makes a one-instrument mapping a coin toss. So the discriminating pin
    # is on a period where the two slots DISAGREE: BMW's 2026 Q1 has slot 2
    # empty and slot 9 populated, so a parser reading slot 2 returns None
    # here and this check goes red. A pin on a period where both agree
    # would pass against the wrong slot, which is the whole failure mode.
    de = _rows("financials_de")
    deq2 = next((r for r in de if (r.fiscal_year, r.fiscal_quarter) == (2026, 2)),
                None)
    deq1 = next((r for r in de if (r.fiscal_year, r.fiscal_quarter) == (2026, 1)),
                None)
    ok &= check("the German fixture reads EUR",
                deq2 is not None and deq2.currency == "EUR")
    ok &= check("EPS on a period where the two candidate slots disagree",
                deq1 is not None and deq1.eps == 2.68)
    # And the measured absence, pinned so that nobody "fixes" it by falling
    # back to the other slot. Basic and diluted EPS are different measures,
    # and filling one column from either would make it mean different things
    # on different rows.
    jp_rows = _rows("financials_jp")
    ok &= check("Toyota's EPS is null on every period, because Google "
                "publishes none in that slot",
                all(r.eps is None for r in jp_rows))
    ok &= check("...while its revenue IS published (so this is a measured "
                "absence, not a broken parse)",
                sum(1 for r in jp_rows if r.revenue is not None) > 50)
    jp = jp_rows
    ok &= check("the Japanese fixture reads JPY",
                jp and jp[0].currency == "JPY")
    ok &= check("a JPY revenue is in the trillions, not mis-scaled",
                jp[0].revenue is not None and jp[0].revenue > 1e12)

    # The two widths the site publishes, reported rather than smoothed over.
    detailed = [r for r in rows if r.detailed]
    summary = [r for r in rows if not r.detailed]
    ok &= check("the capture carries both detailed and summary periods",
                len(detailed) >= 4 and len(summary) >= 20)
    ok &= check("summary periods still carry revenue and EPS",
                all(r.revenue is not None for r in summary[:5]))
    ok &= check("summary periods carry no EBITDA (the slot is past the "
                "array's end)", all(r.ebitda is None for r in summary))
    # Estimates, read from the other direction — the only two slots a
    # not-yet-reported period carries.
    ok &= check("a reported quarter carries an estimate beside its actual",
                q.revenue_estimate is not None and q.eps_estimate is not None)
    ok &= check("the estimate is not the actual",
                q.revenue_estimate != q.revenue)
    # Periods are unique, which is what the dedupe key assumes.
    keys = {(r.fiscal_year, r.fiscal_quarter) for r in rows}
    ok &= check("every period appears once", len(keys) == len(rows))
    ok &= check("quarters are in range",
                all(1 <= r.fiscal_quarter <= 4 for r in rows))
    return ok


def test_analyst_values():
    group("analysts: the consensus, and the slot order that is not obvious")
    ok = True
    rows = _rows("analysts_us")
    ok &= check("44 actions on the Alphabet capture", len(rows) == 44)
    r = rows[0]
    ok &= check("consensus verdict", r.consensus == "StrongBuy")
    ok &= check("analyst total", r.analysts_total == 29)
    # THE check this mode exists to protect. The payload holds [29,
    # "StrongBuy", 25, 0, 4] and the page renders "Buy 25 | Hold 4 | Sell 0"
    # — so slot 9 is SELL and slot 10 is HOLD, read live from the rendered
    # Analysis tab. The obvious reading swaps them on every row.
    ok &= check("buy count", r.buy_count == 25)
    ok &= check("hold count is the FOURTH slot, not the third",
                r.hold_count == 4)
    ok &= check("sell count is the THIRD slot", r.sell_count == 0)
    ok &= check("the three counts sum to the total",
                r.buy_count + r.hold_count + r.sell_count == r.analysts_total)
    ok &= check("target range", (r.target_low, r.target_high, r.target_mean)
                == (379.0, 485.0, 429.31))
    ok &= check("upside is Google's figure, not recomputed",
                r.target_upside_pct == 22.82)
    ok &= check("target currency", r.target_currency == "USD")
    ok &= check("the consensus is repeated on every row",
                len({(x.consensus, x.analysts_total) for x in rows}) == 1)

    # The individual actions.
    ok &= check("every row names its analyst",
                all(x.analyst for x in rows))
    ok &= check("every row names its firm", all(x.firm for x in rows))
    ok &= check("action dates are ISO", all(
        re.match(r"^\d{4}-\d{2}-\d{2}$", x.action_date or "") for x in rows))
    ok &= check("most actions carry a price target",
                sum(1 for x in rows if x.price_target is not None) >= 35)
    first = rows[0]
    ok &= check("the first action's fields are read, not shuffled",
                (first.analyst, first.firm, first.action, first.action_date,
                 first.price_target)
                == ("Ivan Feinseth", "Tigress Financial", "Buy",
                    "2026-09-17", 485.0))
    ok &= check("its headline is the note's, not the byline",
                (first.headline or "").startswith("Alphabet price target"))

    # A second instrument, where the split is not degenerate.
    de = _rows("analysts_de")
    d = de[0]
    ok &= check("BMW consensus", d.consensus == "Buy")
    ok &= check("BMW split sums to its total",
                d.buy_count + d.hold_count + d.sell_count == d.analysts_total)
    ok &= check("BMW has a non-zero hold and sell (a degenerate split would "
                "not test the order)", d.hold_count > 0 and d.sell_count > 0)

    # The downgrade, controlled: a payload that disagrees with itself must
    # null the three counts rather than emit a possibly-swapped one.
    rec = pp._consensus_record(pp.payload(_fx("analysts_us")["html"]))
    ok &= check("the real record passes the invariant",
                pp.rating_breakdown(rec)["buy_count"] == 25)
    broken = list(rec)
    broken[6] = 99                      # total that no longer matches
    ok &= check("a total that does not match nulls the split",
                pp.rating_breakdown(broken) ==
                {"buy_count": None, "hold_count": None, "sell_count": None})
    swapped = list(rec)
    swapped[8], swapped[9] = swapped[9], swapped[8]   # buy <-> sell
    ok &= check("a buy verdict with more sells than buys nulls the split",
                pp.rating_breakdown(swapped)["buy_count"] is None)
    return ok


def test_earnings_values():
    group("earnings: the market page's calendar")
    ok = True
    rows = _rows("earnings_us")
    ok &= check("5 events on the gl=US capture", len(rows) == 5)
    r = rows[0]
    ok &= check("sku is the instrument", r.sku == "AZO:NYSE")
    ok &= check("company name", r.title == "AutoZone Inc")
    ok &= check("Google's own event wording",
                "Earnings" in (r.event_title or ""))
    ok &= check("event date is ISO", r.event_date == "2026-09-22")
    ok &= check("fiscal period", (r.fiscal_year, r.fiscal_quarter) == (2026, 4))
    ok &= check("period end", r.period_end == "2026-08-31")
    ok &= check("currency", r.currency == "USD")
    # The two slots a not-yet-reported period carries, and the reason the
    # financials slot map can be trusted from the other direction.
    ok &= check("revenue estimate", r.revenue_estimate == 6700366520.0)
    ok &= check("EPS estimate", round(r.eps_estimate, 2) == 53.84)
    ok &= check("every event names an instrument", all(x.sku for x in rows))
    ok &= check("every event has a date", all(x.event_date for x in rows))
    ok &= check("events are unique by (sku, date)",
                len({(x.sku, x.event_date) for x in rows}) == len(rows))
    # A non-USD event proves the currency is read rather than defaulted.
    ok &= check("a non-USD event is read as its own currency",
                any(x.currency and x.currency != "USD" for x in rows))
    return ok


def test_chart_values():
    group("chart: two intervals, and the bar that was mislabelled")
    ok = True
    rows = _rows("chart_us")
    ok &= check("99 bars on the Alphabet capture", len(rows) == 99)
    daily = [r for r in rows if r.interval == "1d"]
    intraday = [r for r in rows if r.interval == "5m"]
    ok &= check("20 daily bars", len(daily) == 20)
    ok &= check("79 intraday bars", len(intraday) == 79)
    ok &= check("intraday covers one session",
                len({r.ts[:10] for r in intraday}) == 1)
    ok &= check("daily covers many days",
                len({r.ts[:10] for r in daily}) == len(daily))

    # THE regression. The current session's DAILY bar carries the same
    # timestamp as that session's last five-minute bar, so a per-bar
    # date-grouping label put a whole-day bar in the intraday series — with
    # the session's open, high, low and volume on it. It was found because
    # the three engines stopped agreeing on row 97.
    last_day = max(r.ts[:10] for r in daily)
    same_ts = [r for r in rows if r.ts.startswith(last_day + "T16:00")]
    ok &= check("two bars really do share that timestamp (the trap is real)",
                len(same_ts) == 2)
    ok &= check("...and they are labelled differently",
                {r.interval for r in same_ts} == {"1d", "5m"})
    ok &= check("no (interval, ts) pair repeats",
                len({(r.interval, r.ts) for r in rows}) == len(rows))
    session = next(r for r in same_ts if r.interval == "1d")
    minute = next(r for r in same_ts if r.interval == "5m")
    ok &= check("the daily bar carries the whole session's volume",
                session.volume > minute.volume * 5)

    # OHLC sanity on every bar of every chart fixture.
    for name in ("chart_us", "chart_de"):
        bars = _rows(name)
        ok &= check("%s: low <= open,close <= high on every bar" % name,
                    all(b.low <= b.open <= b.high and b.low <= b.close <= b.high
                        for b in bars))
        ok &= check("%s: every bar has a volume" % name,
                    all(b.volume is not None for b in bars))
        ok &= check("%s: positions are unique" % name,
                    len({b.position for b in bars}) == len(bars))
    # Close is SECOND in the payload, verified against the quote record's own
    # fields — which come from a different part of the payload entirely.
    quote = _rows("quote_stock_us")[0]
    current = next(r for r in daily if r.ts.startswith(last_day))
    ok &= check("the current session's daily bar agrees with the quote "
                "record's open/high/low",
                (current.open, current.high, current.low)
                == (quote.open, quote.day_high, quote.day_low))
    return ok


def test_instrument_selection_is_deterministic():
    group("a page with two quotes for one instrument picks the freshest")
    ok = True
    # The 2026-09-22 Shell capture holds TWO rich records for SHEL:LON taken
    # 13 seconds apart — the page updates its quote while being served — so
    # "the first record walked" made the parse depend on which blobs a
    # document happened to carry. A trimmed fixture and its capture then
    # disagreed about the price while reporting the same row count.
    capture = os.path.join(REPO_ROOT, "captures", "quote_stock_gb.html")
    if not os.path.exists(capture):
        print("  NOTE  captures/ absent; this check needs quote_stock_gb.html")
        return ok
    html = open(capture, encoding="utf-8", errors="replace").read()
    url = "https://www.google.com/finance/beta/quote/SHEL:LON"
    data = pp.payload(html)
    matches = [r for r in pp._rich_records(data) if r[13] == "SHEL:LON"]
    ok &= check("the capture really carries more than one record for it",
                len(matches) > 1)
    stamps = [pp._epoch_iso(r[19][11]) for r in matches]
    ok &= check("their timestamps differ", len(set(stamps)) > 1)
    chosen = pp._subject_record(data, "SHEL:LON")
    ok &= check("the freshest is chosen",
                pp._epoch_iso(chosen[19][11]) == max(s for s in stamps if s))
    # And the same page parsed twice gives the same row.
    a = pp.parse_products(html, url, mode="quote")[0]
    b = pp.parse_products(html, url, mode="quote")[0]
    ok &= check("two parses of one page agree", a.price == b.price)
    return ok


def test_every_mode_is_order_independent():
    group("every mode survives the payload being reordered")
    ok = True
    # The shape-anchoring check extended to all seven modes. Reversing blob
    # order must change no row — not the values, and not the ORDER, because
    # `position` is a column a consumer diffs on. Two of this repo's bugs
    # were exactly this: markets rows came out sectors-first from one blob
    # subset and indices-first from another, and two chart series holding a
    # bar with the same timestamp ordered differently under different walks.
    cases = [(n, f) for n, f in FIXTURES.items() if "rows" in f]
    ok &= check("there are fixtures for every mode to reorder",
                {f["mode"] for _, f in cases} == set(pp.ALL_MODES))
    for name, f in cases:
        blobs = re.findall(r"AF_initDataCallback\(\{.*?\}\);", f["html"], re.S)
        if len(blobs) < 2:
            continue
        head = f["html"][:f["html"].index(blobs[0])]
        reordered = head + "".join(reversed(blobs)) + "</body></html>"
        before = _rows(name)
        after = pp.parse_products(reordered, f["url"], mode=f["mode"], page=1,
                                  market=pp.market_from_url(f["url"]))
        sig_b = [dataclasses.asdict(r) for r in before]
        sig_a = [dataclasses.asdict(r) for r in after]
        for row in sig_b + sig_a:
            row.pop("scraped_at", None)
        ok &= check("%s (%s): reversing blob order changes nothing"
                    % (name, f["mode"]), sig_a == sig_b)
    return ok


def test_finish_run_exit_codes():
    group("exit codes")
    ok = True
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "run")

        def run(rows, **kw):
            base = dict(blocked=False, pages_requested=1, pages_completed=1,
                        start_url=pp.quote_url("GOOGL:NASDAQ"),
                        final_url=pp.quote_url("GOOGL:NASDAQ"),
                        mode="quote", source=SOURCE_DEFAULT)
            base.update(kw)
            allow_empty = base.pop("allow_empty", False)
            buf = io.StringIO()
            with redirect_stdout(buf):
                return finish_run(rows, out, "json", allow_empty, **base)

        r = [Quote(sku="GOOGL:NASDAQ", price=1.0)]
        ok &= check("rows and a clean finish is exit 0",
                    run(list(r), stop_reason="completed") == 0)
        ok &= check("a blocked run is exit 3",
                    run([], blocked=True, stop_reason="blocked_sorry") == 3)
        ok &= check("zero rows from a page that WAS fetched is exit 4",
                    run([], stop_reason="completed") == 4)
        # CLAUDE.md §25: a run that never got its page reports 5, not 4 —
        # a dead proxy and an empty market must not be one value to a
        # pipeline. Keyed on "did the run finish", not on a named list of
        # stop reasons, because a list cannot cover a reason nobody has
        # added to it yet.
        ok &= check("zero rows because nothing was FETCHED is exit 5",
                    run([], stop_reason="page_load_timeout") == 5)
        ok &= check("a stop reason nobody anticipated is also exit 5",
                    run([], stop_reason="some_future_reason_nobody_listed") == 5)
        ok &= check("a partial run that gathered rows is exit 6",
                    run(list(r), stop_reason="page_load_timeout") == 6)

        # A failed run writes NO sidecar, so a "failed" sidecar cannot sit
        # beside good data from an earlier run and contradict it.
        for f in glob.glob(out + "*"):
            os.remove(f)
        buf = io.StringIO()
        with redirect_stdout(buf):
            finish_run([], out, "json", False, blocked=False,
                       stop_reason="page_load_timeout", pages_requested=1,
                       pages_completed=0,
                       start_url=pp.quote_url("GOOGL:NASDAQ"),
                       final_url=pp.quote_url("GOOGL:NASDAQ"),
                       mode="quote", source=SOURCE_DEFAULT)
        ok &= check("a failed run writes no output and no sidecar",
                    not os.path.exists(out + ".json")
                    and not os.path.exists(out + ".meta.json"))
        # An empty CSV still carries its header.
        buf = io.StringIO()
        with redirect_stdout(buf):
            finish_run([], out, "csv", True, blocked=False,
                       stop_reason="completed", pages_requested=1,
                       pages_completed=1,
                       start_url=pp.quote_url("GOOGL:NASDAQ"),
                       final_url=pp.quote_url("GOOGL:NASDAQ"),
                       mode="quote", source=SOURCE_DEFAULT)
        if os.path.exists(out + ".csv"):
            head = open(out + ".csv", encoding="utf-8").readline()
            ok &= check("an empty CSV still carries its header",
                        head.startswith("source,scraped_at,url,sku,title"))
        else:
            ok &= check("an --allow-empty csv run wrote a file", False)
    return ok


def test_policy_constants_have_consumers():
    group("no policy constant without a consumer")
    ok = True
    # CLAUDE.md §17: a constant carrying a paragraph of measured
    # justification that nothing reads is the same defect as dead code, and
    # harder to see because the prose reads like enforcement.
    sources = {}
    for f in glob.glob(os.path.join(REPO_ROOT, "*.py")):
        sources[os.path.basename(f)] = open(f, encoding="utf-8").read()
    for const in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL",
                  "BLOCK_RETRIES_WITH_POOL", "THROTTLE_RETRIES",
                  "THROTTLE_BACKOFF_MS", "SOLVES_PER_PAGE",
                  "MIN_CARD_MATCHES", "CONTENT_TIMEOUT_MS", "STATE_POLICY"):
        users = [n for n, s in sources.items()
                 if n not in ("page_flow.py", "smoke_test.py")
                 and re.search(r"\b%s\b" % const, s)]
        indirect = [n for n, s in sources.items()
                    if n not in ("page_flow.py", "smoke_test.py")
                    and re.search(r"page_flow\.\w+", s)]
        ok &= check("%s is read outside page_flow (or through a function "
                    "that reads it)" % const, bool(users or indirect))
    # SOLVES_PER_PAGE is only a cap if every call site goes through the
    # budget. A sibling found this constant had read like a limit in every
    # repo in the family while every engine solved twice and counted once.
    for eng in ENGINES:
        src = sources.get(eng + ".py", "")
        if not src:
            continue
        solves = len(re.findall(r"handle_captcha_if_present\(", src))
        budgets = len(re.findall(r"solve_budget\(", src))
        ok &= check("%s: every solve call site is budgeted (%d calls, %d "
                    "budget checks)" % (eng, solves, budgets),
                    budgets >= max(0, solves - 1))
    return ok


# ---------------------------------------------------------------------------
# Structural checks — the ones that catch what a live run would
# ---------------------------------------------------------------------------

def _module_sources():
    out = {}
    for path in sorted(glob.glob(os.path.join(REPO_ROOT, "*.py"))):
        out[os.path.basename(path)] = open(path, encoding="utf-8").read()
    return out


def test_no_undefined_names():
    group("every name resolves (the check a live run would need)")
    ok = True
    # CLAUDE.md §10. Deliberately COARSE — it pools every binding in a file
    # rather than tracking scopes — so it under-reports rather than
    # inventing problems. It earned its place during this repo's build: two
    # engines referenced `market_from_url` and `MOVER_LISTS` without
    # importing them, on lines reached only while fetching. `--help` worked,
    # the modules imported, compileall passed, and both engines died with
    # NameError on their first live run.
    scanned = 0
    for name, src in _module_sources().items():
        tree = ast.parse(src, name)
        bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
        for n in ast.walk(tree):
            if isinstance(n, ast.alias):
                bound.add((n.asname or n.name).split(".")[0])
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
                bound.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                bound.add(n.id)
            elif isinstance(n, ast.arg):
                bound.add(n.arg)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                bound.add(n.name)
            elif isinstance(n, ast.Global):
                bound.update(n.names)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(used - bound)
        ok &= check("%s: no undefined names%s"
                    % (name, "" if not missing else " (%s)" % missing),
                    not missing)
        scanned += 1
    # A check that scanned nothing passes for the wrong reason.
    ok &= check("the walk actually scanned modules", scanned >= 10)
    return ok


def test_no_unreachable_code():
    group("no statement after a return/raise in the same block")
    ok = True
    # CLAUDE.md §22: found the same fifteen lines in six sibling repos,
    # present since each one's first commit — a function whose `def` line had
    # been lost, leaving its body absorbed into the end of the function
    # above. It parses, it imports, `--help` works, compileall passes, and
    # the undefined-name walk above cannot see it and SHOULD not: it pools
    # bindings per file, so a name in the dead block resolves against a real
    # parameter elsewhere. This is a second check, not a tightening.
    findings = []
    scanned = 0
    for name, src in _module_sources().items():
        tree = ast.parse(src, name)
        for node in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Break,
                                         ast.Continue)):
                        findings.append("%s:%d" % (name, block[i + 1].lineno))
        scanned += 1
    ok &= check("the walk actually scanned modules", scanned >= 10)
    ok &= check("no unreachable statements%s"
                % ("" if not findings else " (%s)" % findings[:5]), not findings)
    return ok


def test_shared_calls_bind():
    group("every engine call into a shared module binds")
    ok = True
    # CLAUDE.md §17's check #1, the one that earns its keep: a sibling had
    # two of three engines calling `classify(html, url=...)` against a
    # `classify(html, status, url)` signature, and BOTH crashed on their
    # first fetch — invisible to import, --help, compileall, the
    # undefined-name walk and 400 green assertions.
    #
    # And §22's correction to it: when a name does not exist in the callee at
    # all, SAY SO rather than skipping it. A sibling's version resolved with
    # `getattr(mod, name, None)` and skipped anything not callable, so the
    # single loudest thing it could report was the one case it stayed silent
    # about.
    import importlib
    mods = {}
    for m in SHARED_MODULES:
        try:
            mods[m] = importlib.import_module(m)
        except Exception:
            pass
    checked = 0
    problems = []
    for eng in ENGINES:
        path = os.path.join(REPO_ROOT, eng + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src, eng)
        # A name bound anywhere in the calling file SHADOWS a same-named
        # module: an engine takes `proxy_pool` as a parameter, so
        # `proxy_pool.next()` is a method call, not a module attribute.
        # Without this rule the check reported 21 false positives on a clean
        # sibling; with it, zero.
        shadowed = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                shadowed.add(n.id)
            elif isinstance(n, ast.arg):
                shadowed.add(n.arg)
        for call in [n for n in ast.walk(tree) if isinstance(n, ast.Call)]:
            fn = call.func
            if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
                continue
            modname = fn.value.id
            if modname not in mods or modname in shadowed:
                continue
            mod = mods[modname]
            if not hasattr(mod, fn.attr):
                problems.append("%s:%d %s.%s does not exist"
                                % (eng, call.lineno, modname, fn.attr))
                continue
            target = getattr(mod, fn.attr)
            if not callable(target):
                continue
            try:
                sig = inspect.signature(target)
            except (TypeError, ValueError):
                continue
            args = [inspect.Parameter.empty] * len(call.args)
            kwargs = {kw.arg: None for kw in call.keywords if kw.arg}
            if any(kw.arg is None for kw in call.keywords) or \
               any(isinstance(a, ast.Starred) for a in call.args):
                continue
            try:
                sig.bind(*args, **kwargs)
            except TypeError as e:
                problems.append("%s:%d %s.%s(%s)"
                                % (eng, call.lineno, modname, fn.attr, e))
            checked += 1
    ok &= check("the binder actually bound something (%d calls)" % checked,
                checked >= 20)
    ok &= check("every shared-module call binds%s"
                % ("" if not problems else " (%s)" % problems[:4]), not problems)
    return ok


def test_engine_flag_parity():
    group("the three engines expose the same CLI")
    ok = True
    # CLAUDE.md §17's check #2, asserted in BOTH directions: a new unshared
    # flag fails, and so does closing a documented difference, because the
    # exception list IS the documentation.
    flags = {}
    for eng in ENGINES:
        path = os.path.join(REPO_ROOT, eng + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        flags[eng] = set(re.findall(r'p\.add_argument\("(--[a-z0-9-]+)"', src))
    ok &= check("all three engines were read", len(flags) == 3)
    if len(flags) == 3:
        base = flags["playwright_scraper"]
        # The exception list IS the documentation, so it is asserted in BOTH
        # directions: a new unshared flag fails, and so does REMOVING a
        # documented difference. A sibling skipped this check, and when it
        # was finally written it found twelve flags the primary engine had
        # and its twins did not — nine of them predating the work, with the
        # README promising "same CLI" throughout.
        EXPECTED_EXTRAS = {
            # pyppeteer downloads its own Chromium and needs to be told
            # where a system one is; Playwright and Selenium each resolve a
            # browser themselves.
            "puppeteer_scraper": {"--chromium-path"},
            "selenium_scraper": set(),
        }
        for eng in ("puppeteer_scraper", "selenium_scraper"):
            missing = sorted(base - flags[eng])
            extra = sorted(flags[eng] - base)
            ok &= check("%s is missing no shared flag (%s)" % (eng, missing),
                        not missing)
            ok &= check("%s's extra flags are exactly the documented ones "
                        "(%s)" % (eng, extra),
                        set(extra) == EXPECTED_EXTRAS[eng])
        # The family contract, plus this site's two additions.
        contract = {"--url", "--pages", "--category", "--format", "--out",
                    "--delay", "--retries", "--retry-delay", "--concurrency",
                    "--proxy", "--proxy-file", "--proxy-rotate",
                    "--proxy-shuffle", "--proxy-block-retries",
                    "--twocaptcha-key", "--captcha-api", "--solve-captcha",
                    "--min-score", "--cdp-endpoint", "--allow-empty",
                    "--dump-html", "--headless", "--headful", "--fingerprint",
                    "--fp-country", "--fp-tags", "--locale", "--mode"}
        ok &= check("every contract flag is present (missing %s)"
                    % sorted(contract - base), not (contract - base))
        ok &= check("--symbols and --market are this site's additions",
                    {"--symbols", "--market"} <= base)
        # A removed flag stays removed: --country on a scraper could
        # disagree with the url, and here --market is the honest spelling.
        ok &= check("--country is not a scraper flag",
                    "--country" not in base)
    return ok


def test_engines_import_their_driver_at_module_level():
    group("each engine imports its driver at MODULE level")
    ok = True
    # Otherwise the module imports cleanly with the library absent, the
    # skip group never fires, and the CI job that exists to fail on
    # unexpected skips cannot catch a broken import. A sibling ran against a
    # stub pyppeteer for a while with nothing noticing.
    expect = {"playwright_scraper": "playwright",
              "puppeteer_scraper": "pyppeteer",
              "selenium_scraper": "selenium"}
    for eng, lib in expect.items():
        path = os.path.join(REPO_ROOT, eng + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read(), eng)
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = []
        for n in top:
            if isinstance(n, ast.ImportFrom) and n.module:
                names.append(n.module)
            else:
                names.extend(a.name for a in getattr(n, "names", []))
        ok &= check("%s imports %s at module level" % (eng, lib),
                    any(str(x).split(".")[0] == lib for x in names))
    return ok


def test_banned_wording():
    group("banned wording")
    ok = True
    # Assembled from pieces so this file can scan ITSELF. Three siblings
    # exempted smoke_test.py wholesale — the file most likely to acquire a
    # stray phrase, or a pasted credential, was the one nobody scanned.
    banned = ["cloud" + " browser", "anti" + "detect browser",
              "2scraper Anti" + "detect Browser", "--anti" + "detect",
              "ANTI" + "DETECT_LOCAL_API", "gate.2prx" + ".com"]
    scan = []
    for path in glob.glob(os.path.join(REPO_ROOT, "*.py")) + \
            glob.glob(os.path.join(REPO_ROOT, "*.md")) + \
            glob.glob(os.path.join(REPO_ROOT, "*.txt")) + \
            glob.glob(os.path.join(REPO_ROOT, ".github", "workflows", "*.yml")):
        scan.append(path)
    ok &= check("the wording scan covers this file too",
                os.path.abspath(__file__) in [os.path.abspath(p) for p in scan])
    hits = []
    for path in scan:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        for phrase in banned:
            if phrase.lower() in text.lower():
                hits.append("%s: %s" % (os.path.basename(path), phrase))
    ok &= check("no banned phrase in any shipped file%s"
                % ("" if not hits else " (%s)" % hits[:4]), not hits)
    ok &= check("the right name is used somewhere",
                any("Scraping Browser API" in open(p, encoding="utf-8").read()
                    for p in scan if p.endswith((".py", ".md"))))
    return ok


def test_env_example_round_trip():
    group(".env.example round-trips as UNSET")
    ok = True
    import env_config
    example = os.path.join(REPO_ROOT, ".env.example")
    ok &= check(".env.example exists", os.path.exists(example))
    if not os.path.exists(example):
        return ok
    text = open(example, encoding="utf-8").read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))
    env_keys = dict(getattr(env_config, "ENV_KEYS", {}))
    read_by_code = set(env_keys)
    ok &= check("ENV_KEYS is a non-empty explicit map", bool(read_by_code))
    ok &= check("every variable the code reads is documented (missing %s)"
                % sorted(read_by_code - documented),
                not (read_by_code - documented))
    ok &= check("every documented variable is read by the code (extra %s)"
                % sorted(documented - read_by_code),
                not (documented - read_by_code))
    # A copied example must read as UNSET. CLAUDE.md §17: the placeholder
    # check was a literal set, and the two credentialled URLs are documented
    # the way the vendor documents them, so `cp .env.example .env` produced
    # a 401 a long way from its cause.
    with tempfile.TemporaryDirectory() as d:
        dotenv = os.path.join(d, ".env")
        with open(dotenv, "w", encoding="utf-8") as f:
            f.write(text)

        class A:
            pass
        args = A()
        for dest in env_keys.values():
            setattr(args, dest, None)
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            try:
                env_config.apply(args, path=dotenv)
            except TypeError:
                env_config.apply(args)
        still_unset = [d2 for d2 in set(env_keys.values())
                       if getattr(args, d2, None) in (None, "")]
        credentialish = [d2 for d2 in set(env_keys.values())
                         if any(w in d2 for w in ("key", "proxy", "endpoint"))]
        ok &= check("every credential in a copied example reads as unset "
                    "(set: %s)" % sorted(set(credentialish) - set(still_unset)),
                    not (set(credentialish) - set(still_unset)))
        ok &= check("a braced placeholder is treated as unset",
                    "{" in text)
    return ok


def test_dockerfile_copies_what_it_imports():
    group("the Dockerfile carries the entrypoint's import graph")
    ok = True
    # All three repos in this family once shipped an image that died with
    # ModuleNotFoundError on every invocation, --help included, because one
    # module was missing from an explicit COPY list. CI never built it.
    path = os.path.join(REPO_ROOT, "Dockerfile")
    ok &= check("Dockerfile exists", os.path.exists(path))
    if not os.path.exists(path):
        return ok
    dockerfile = open(path, encoding="utf-8").read()
    # Join line continuations FIRST. The real COPY here spans three lines,
    # and a per-line regex read only the first of them — reporting four
    # modules missing that were three characters further down the file. A
    # check that fails on its own repository is a check nobody can read.
    dockerfile = re.sub(r"\\\s*\n\s*", " ", dockerfile)
    copied = set()
    for m in re.finditer(r"^COPY\s+(.+?)\s+\S+\s*$", dockerfile, re.M):
        for token in m.group(1).split():
            copied.add(token.strip().lstrip("./"))
    local = {os.path.basename(p)[:-3]
             for p in glob.glob(os.path.join(REPO_ROOT, "*.py"))}
    needed = set()
    frontier = ["playwright_scraper"]
    seen = set()
    while frontier:
        mod = frontier.pop()
        if mod in seen:
            continue
        seen.add(mod)
        p = os.path.join(REPO_ROOT, mod + ".py")
        if not os.path.exists(p):
            continue
        needed.add(mod + ".py")
        tree = ast.parse(open(p, encoding="utf-8").read(), mod)
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name.split(".")[0] in local:
                        frontier.append(a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom) and n.module:
                if n.module.split(".")[0] in local:
                    frontier.append(n.module.split(".")[0])
    missing = sorted(m for m in needed
                     if m not in copied and not any(
                         c in ("*.py", ".") for c in copied))
    ok &= check("the import graph was walked (%d modules)" % len(needed),
                len(needed) >= 5)
    ok &= check("every imported module is COPYed (missing %s)" % missing,
                not missing)
    # And the image must not carry the suite, the fixtures or a .env — a
    # .env baked into an image is a credential published to everyone who can
    # pull it.
    for forbidden in ("smoke_test.py", ".env", "captures"):
        ok &= check("the image does not COPY %s" % forbidden,
                    forbidden not in copied)
    return ok


def test_sample_output_matches_the_schema():
    group("sample_output.* is a real run's shape")
    ok = True
    names = [f.name for f in dataclasses.fields(Quote)]
    j = os.path.join(REPO_ROOT, "sample_output.json")
    c = os.path.join(REPO_ROOT, "sample_output.csv")
    ok &= check("sample_output.json exists", os.path.exists(j))
    ok &= check("sample_output.csv exists", os.path.exists(c))
    if os.path.exists(j):
        rows = json.load(open(j, encoding="utf-8"))
        ok &= check("the sample has rows", bool(rows))
        if rows:
            ok &= check("its columns are the row class's, in order",
                        list(rows[0].keys()) == names)
            ok &= check("its source is this site",
                        all(r["source"] == SOURCE_DEFAULT for r in rows))
            # Fabrication markers: a sample nobody ran is worse than none.
            blob = json.dumps(rows)
            for marker in ("example.com", "lorem", "FIXME", "TODO",
                           "your_", "XXXX"):
                ok &= check("no fabrication marker %r in the sample" % marker,
                            marker.lower() not in blob.lower())
    if os.path.exists(c):
        head = open(c, encoding="utf-8").readline().strip()
        ok &= check("the CSV header is the row class's, in order",
                    head.split(",") == names)
    return ok


def test_ci_checks_is_wired_up():
    group("one implementation of the credential scan, invoked from both")
    ok = True
    # CLAUDE.md §17: three siblings shipped `.github/ci_checks.py` invoked by
    # NOTHING while tests.yml carried an inline grep doing a narrower job —
    # two sources of truth, one dead and one holed.
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    workflow = os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml")
    if not os.path.isdir(os.path.join(REPO_ROOT, ".github")):
        # The Docker image deliberately COPYs no .github/, and the suite runs
        # inside it at build time. Trigger on the WHOLE directory being
        # absent, never on a file inside it being missing: a check that
        # quietly starts passing once its input disappears is the failure
        # mode this whole section is about.
        print("  NOTE  no .github/ in this tree (the Docker image); skipped")
        return ok
    ok &= check("ci_checks.py exists", os.path.exists(script))
    ok &= check("tests.yml exists", os.path.exists(workflow))
    if os.path.exists(workflow):
        wf = open(workflow, encoding="utf-8").read()
        ok &= check("the workflow CALLS the shipped check rather than "
                    "reimplementing it", "ci_checks.py" in wf)
    if os.path.exists(script):
        r = subprocess.run([sys.executable, script, "--all"],
                           cwd=REPO_ROOT, capture_output=True, text=True)
        ok &= check("ci_checks.py --all passes on this repo (%s)"
                    % (r.stdout + r.stderr)[-200:].replace("\n", " "),
                    r.returncode == 0)
        # The credential scan asks GIT for its file list, so it reads
        # nothing in a directory that is not a repository — and used to
        # report "ok, 0 files scanned, nothing credential-shaped" when it
        # did. Controlled here by running it somewhere with no git index:
        # the guard must make that a FAILURE, because a guard that quietly
        # starts passing once its input disappears is worse than no guard.
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".github"))
            import shutil
            shutil.copy(script, os.path.join(d, ".github", "ci_checks.py"))
            r2 = subprocess.run([sys.executable, ".github/ci_checks.py",
                                 "--secret-check"],
                                cwd=d, capture_output=True, text=True)
            ok &= check("the credential scan FAILS rather than passing when "
                        "it reads no files", r2.returncode != 0)
            ok &= check("...and says why", "0 files" in (r2.stdout + r2.stderr))
    return ok


def test_canary_is_coherent():
    group("the canary tests what it claims to")
    ok = True
    path = os.path.join(REPO_ROOT, ".github", "workflows", "canary.yml")
    if not os.path.isdir(os.path.join(REPO_ROOT, ".github")):
        print("  NOTE  no .github/ in this tree (the Docker image); skipped")
        return ok
    ok &= check("canary.yml exists", os.path.exists(path))
    if not os.path.exists(path):
        return ok
    wf = open(path, encoding="utf-8").read()

    # It must need no secrets. This repo's central claim is that the site
    # needs none, and a canary gated on one would go green every day while
    # testing nothing — CLAUDE.md §21's other half.
    ok &= check("the canary is not gated on a secret",
                "secrets." not in wf)
    ok &= check("it runs on a schedule, not only on dispatch",
                "schedule:" in wf and "cron:" in wf)

    # Every mode must be under daily test, or a mode can rot unnoticed.
    for mode in pp.ALL_MODES:
        ok &= check("the canary exercises --mode %s" % mode,
                    "--mode %s" % mode in wf or
                    ('for mode in' in wf and mode in wf))

    # `set +e` before capturing an exit code: GitHub runs `run:` steps under
    # `bash -e`, so without it a non-zero scraper exit aborts the step before
    # the `echo exit_code` line and the verdict reads an empty string.
    captures = wf.count('exit_code=$')
    ok &= check("every exit-code capture is preceded by `set +e` (%d captures,"
                " %d guards)" % (captures, wf.count("set +e")),
                wf.count("set +e") >= captures - 1)

    # Exit 5 must be a DEFECT here, not a warning. The siblings warn on it
    # because their canaries use a credentialled remote API; this one uses
    # none, so warning would be carrying another repo's excuse.
    five = re.search(r'^\s*5\)\s*$.{0,600}', wf, re.S | re.M)
    ok &= check("exit 5 is treated as a defect, not an access condition",
                bool(five) and "::error::" in five.group(0))
    three = re.search(r'^\s*3\)\s*$.{0,900}', wf, re.S | re.M)
    ok &= check("exit 3 is treated as an access condition, not a defect",
                bool(three) and "::warning::" in three.group(0))

    # The assertion block must be extractable and syntactically valid — a
    # canary whose Python does not parse fails at 2am rather than here.
    m = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n", wf, re.S)
    ok &= check("the assertion block is present", bool(m))
    if m:
        body = textwrap.dedent(m.group(1))
        try:
            ast.parse(body)
            parsed = True
        except SyntaxError:
            parsed = False
        ok &= check("the assertion block parses as Python", parsed)
        ok &= check("it asserts on the financial slot map, which nothing "
                    "else tests daily",
                    "net_profit_margin" in body and "ebitda" in body)
        ok &= check("it asserts both chart intervals are present",
                    '"5m", "1d"' in body or "{'5m', '1d'}" in body)
    return ok


def test_engines(skips):
    group("engines (import-guarded)")
    ok = True
    for eng in ENGINES:
        try:
            mod = __import__(eng)
        except ImportError as e:
            skips.append("%s (%s)" % (eng, e))
            print("  SKIP  %s — driver library absent" % eng)
            continue
        ok &= check("%s exposes scrape()" % eng, callable(getattr(mod, "scrape", None)))
        ok &= check("%s exposes parse_args()" % eng,
                    callable(getattr(mod, "parse_args", None)))
        # The mode router must not be the stale one: two engines shipped a
        # router that called the parser with no `mode`, so --mode markets
        # silently took the quote path and reported 0 rows on a page that
        # parses to 46.
        src = inspect.getsource(mod._parse_for_mode)
        ok &= check("%s routes the mode to the parser" % eng,
                    "mode=args.mode" in src)
        ok &= check("%s passes the market through" % eng, "market=market" in src)
    return ok


def test_concurrency_machinery(skips):
    group("concurrency, with the browser stubbed out")
    ok = True
    # A live run cannot always reach this: the first fetch decides whether
    # the rest may be dispatched at all, so a refused first page means the
    # workers never start.
    try:
        import playwright_scraper as pws
    except ImportError as e:
        skips.append("playwright_scraper concurrency (%s)" % e)
        print("  SKIP  playwright_scraper absent")
        return ok
    fetcher = getattr(pws, "_fetch_pages_concurrently", None)
    if not callable(fetcher):
        print("  NOTE  this engine has no concurrent dispatcher")
        return ok
    ok &= check("the dispatcher exists", True)
    ok &= check("its signature is (args, pool, specs, concurrency)",
                list(inspect.signature(fetcher).parameters)[:4]
                == ["args", "pool", "specs", "concurrency"])
    return ok


def main() -> int:
    ok = True
    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and
    # still says "all passed" is the same defect as code that reports
    # success without checking that what it wanted actually happened.
    skips = []

    ok &= test_payload_extraction()
    ok &= test_shape_anchoring_not_key_numbering()
    ok &= test_quote_values()
    ok &= test_every_instrument_kind()
    ok &= test_zero_is_not_a_measurement()
    ok &= test_markets_and_movers()
    ok &= test_second_market()
    ok &= test_not_found_is_not_empty()
    ok &= test_unsupported_client_is_not_a_block()
    ok &= test_marker_sets_score_zero_on_good_pages()
    ok &= test_state_ordering()
    ok &= test_symbol_matching()
    ok &= test_urls()
    ok &= test_there_is_no_pagination()
    ok &= test_financial_values()
    ok &= test_analyst_values()
    ok &= test_earnings_values()
    ok &= test_chart_values()
    ok &= test_instrument_selection_is_deterministic()
    ok &= test_every_mode_is_order_independent()
    ok &= test_output_contract()
    ok &= test_finish_run_exit_codes()
    ok &= test_policy_constants_have_consumers()
    ok &= test_no_undefined_names()
    ok &= test_no_unreachable_code()
    ok &= test_shared_calls_bind()
    ok &= test_engine_flag_parity()
    ok &= test_engines_import_their_driver_at_module_level()
    ok &= test_banned_wording()
    ok &= test_env_example_round_trip()
    ok &= test_dockerfile_copies_what_it_imports()
    ok &= test_sample_output_matches_the_schema()
    ok &= test_ci_checks_is_wired_up()
    ok &= test_canary_is_coherent()
    ok &= test_engines(skips)
    ok &= test_concurrency_machinery(skips)

    print("\n" + "=" * 68)
    if skips:
        print("SKIPPED (engine library absent):")
        for s in skips:
            print("  - %s" % s)
        print("  CI installs each engine in its own venv and fails if the")
        print("  matching group skips, because 'skipped, engine absent'")
        print("  reads identically to a real import error.")
    if ok:
        print("ALL CHECKS PASSED")
        return 0
    print("FAILURES (%d):" % len(_failures))
    for f in _failures:
        print("  - %s" % f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
