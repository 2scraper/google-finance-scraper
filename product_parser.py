"""Google Finance — parsing, page classification and site knowledge.

This is the one module that knows what Google Finance is. Everything else in
the repo is family core and site-agnostic; if you find yourself writing site
knowledge anywhere else, it belongs here.

Every number in this docstring was measured on 2026-09-22 from a Hetzner
datacentre address in Helsinki (AS24940), with plain HTTP and a Chrome
User-Agent — no key, no proxy, no browser — against 11 captures covering six
instrument kinds and six markets. Re-measure before quoting any of them; a
count that rots is worse than no count.

WHAT THE SITE SERVES
--------------------
`www.google.com/finance/*` 302s to `/finance/beta/*`. The beta app renders
its whole payload into `AF_initDataCallback({key: 'ds:N', data:[...]})`
blobs — 22 to 26 of them on a quote page, 15 on the root — as plain nested
JSON arrays, fully server-rendered. There is no JSON-LD anywhere: measured
**zero** `application/ld+json` blocks on all 11 captures, so CLAUDE.md §4's
primary path does not exist here and the payload IS the primary path.

NEVER ANCHOR ON `ds:N`
----------------------
The key numbering is an artefact of render order, not a contract. Measured:
stable across three consecutive fetches of the SAME url, and it SHIFTS the
moment a query parameter changes the page — `?window=5D` and `?window=1Y`
produce 23 keys instead of 22 and move every key up by one, so the daily
series that is `ds:12` on a bare quote url is `ds:13` on a windowed one.

That is the same class of mistake as anchoring a parser on a build-hash CSS
class. Everything below therefore finds its data by the SHAPE of the record
— a tuple whose slot 21 is the canonical `TICKER:EXCHANGE` string and whose
slot 1 is a two-element `[ticker, exchange]` pair is an instrument record,
wherever it happens to live this week.

THE CLIENT IS THE GATE, AND ITS SIGN IS INVERTED FROM ITS SIBLINGS
------------------------------------------------------------------
Same address, same url, one fetch each:

    curl, curl's own User-Agent   ->  /finance/beta/unsupported, 657 KB, no data
    curl, a Chrome User-Agent     ->  the real page, 1.31 MB
    headless Chromium             ->  the real page

`Sec-Fetch-Dest/Mode/Site` make no difference — tested with and without, both
follow the User-Agent. So on this site a browser User-Agent is REQUIRED,
where a sibling site in this family refuses curl precisely FOR wearing
one. Neither
is a general rule; the axis is what to measure, not the direction.

WHAT IS NOT HERE, AND WHY
-------------------------
No pagination, on any of the three modes. The `/finance/markets/{indexes,
gainers,losers,most-active,cryptocurrencies,currencies,climate-leaders}`
pages, and their `/beta/` equivalents, ALL redirect to the `/finance/beta/`
root, which carries every list in one payload — checked on 2026-09-22, seven
paths, seven redirects. A quote page is one instrument and a list is however
many rows the root publishes. `paginates_by_url()` therefore answers False
for every url in this repo, and the engines refuse `--pages > 1` and
`--concurrency > 1`-over-pages with that reason rather than fetching the
same page repeatedly and calling it a listing.

The unit of work is the SYMBOL, not the page: `--symbols A,B,C` in
`--mode quote` is what the family's page loop iterates, and `page` on a row
is the 1-based index of its symbol within that run.
"""

from __future__ import annotations

import html as _html
import json
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse, urlencode, urlunparse

from output_writer import Quote

# --------------------------------------------------------------------------
# Site identity
# --------------------------------------------------------------------------

SITE_HOST = "www.google.com"

# Every host this repo will fetch. Google Finance is served from the one
# host: unlike the country-domain families in this repo's siblings, the
# market is chosen by the `gl` QUERY PARAMETER rather than by a TLD, so
# there is no per-country host table to derive or to get wrong.
HOSTS = ("www.google.com", "google.com")

# The path prefix the site redirects every older spelling to. `/finance/` and
# `/finance/markets/...` both 302 here, so this repo asks for it directly and
# saves a redirect rather than pretending the old paths still mean anything.
BETA_PREFIX = "/finance/beta"

# The market a run reads, as `gl`. Measured 2026-09-22 from one Helsinki
# address, one fetch each: `gl=US` returns NASDAQ movers and CBOE sector
# indices, `gl=DE` returns ETR, and no parameter at all returns HEL — the
# exit IP's own market. That is the whole reason this repo can honestly say
# a proxy is not required to read a named market: the market is a query
# parameter, not a property of the address.
MARKET_PARAM = "gl"
LANGUAGE_PARAM = "hl"

# Markets confirmed to return a populated root page. Not an allowlist — any
# two-letter code is passed through — but the ones a live run has been seen
# to work on, and the note that `BR` returned a root page with NO movers
# blob at all is why `parse_movers` treats an absent list as empty rather
# than as a failure.
MARKETS_SEEN = ("US", "DE", "GB", "JP", "IN", "FI", "BR")

# The family's `SELECTORS` map. Google Finance renders its grid from the
# payload, so these exist for the engines' readiness wait rather than for
# extraction — nothing below reads the DOM for data.
SELECTORS = {
    # The quote header, present once a quote page has painted.
    "quote_ready": '[data-entityid], main [role="main"]',
    # A list row on the root page.
    "list_ready": 'main a[href*="/finance/beta/quote/"]',
    # Used only by `expected_cards`; see the engines.
    "item_link": 'a[href*="/finance/quote/"], a[href*="/finance/beta/quote/"]',
}

# `hl` values this repo has fetched. The site accepts far more; these are the
# ones a capture exists for.
LOCALES = ("en", "de")

CURRENCY = None  # never a default; see `currency_from_page`.

# --------------------------------------------------------------------------
# Refusal, challenge and not-found markers
# --------------------------------------------------------------------------

# Google's OWN refusal vocabulary. Every one of these was counted on the 11
# captures of 2026-09-22 and scores **0 on all nine served pages**, 0 on the
# Page Not Found page and 0 on the unsupported-browser page — which is what
# makes them markers rather than facts about the site (CLAUDE.md §18: a
# marker that matches every page is worse than no marker).
#
# No challenge was met in testing, from this address, across those 11
# fetches. That is emphatically NOT a claim that Google does not challenge
# scrapers: it serves `/sorry/index` with a reCAPTCHA to addresses it does
# not like, which is exactly what this set is built to recognise. It is a
# claim about what one address saw on one day.
BOT_CHALLENGE_MARKERS = (
    # The interstitial itself, by path and by its own form field.
    "/sorry/index",
    "CaptchaRedirect",
    # Its visible copy, in the two spellings Google uses.
    "Our systems have detected unusual traffic",
    "unusual traffic from your computer network",
    # The widget the sorry page mounts. Specific loader paths only: a bare
    # `captcha` or `recaptcha` marker is the trap CLAUDE.md §24 measured,
    # firing 21 and 2 times on a good page fetched over a managed browser
    # whose auto-solve extension injects its own hunters.
    "recaptcha/api.js",
    "recaptcha/api2/anchor",
    "recaptcha/api2/bframe",
)

# The site's own Page Not Found, as it spells it. An UNAMBIGUOUS POSITIVE
# SIGNAL — no interstitial and no served quote page carries it — and it is
# read BEFORE any row is counted, which is not a stylistic choice:
#
#   a Not Found page still parses to 35 instrument records.
#
# The page chrome carries the index and sector strips whatever the url asked
# for, so "did we get rows?" answers YES on a page holding no such
# instrument. Classifying first is what stops a run reporting 35 rows of
# furniture as a successful quote fetch.
NOT_FOUND_MARKERS = (
    'title="Page Not Found"',
    ">Page Not Found<",
)

# The UA gate. Structural rather than textual: the page declares itself in
# its own canonical link and loads a module named for it, and neither is
# locale-dependent the way visible copy would be.
UNSUPPORTED_MARKERS = (
    "/finance/beta/unsupported",
    "m=unsupportedview",
)

# THE REGIONAL REFUSAL, which is not a block of this address's reputation
# but of its COUNTRY.
#
# Google answers a request from a country it does not serve Finance in with
# HTTP 403 and its own sentence. Reported by a third-party audit on
# 2026-09-24 and not reproducible from this repo's own exits — Helsinki and
# GitHub's runners are both in supported regions — which is exactly why it
# needs a marker rather than a measurement: the condition is invisible from
# where the code was written.
#
# Matched on the BODY, not only on the status, and that is deliberate:
# selenium_scraper.py cannot obtain an HTTP status at all, so a
# status-only rule would leave one of three engines calling this `unknown`
# and retrying it. CLAUDE.md §8: take the status where it is available and
# find a STRUCTURAL signal for where it is not.
REGION_MARKERS = (
    "not supported in your region",
    "currently not supported in your",
)

# A served page is built out of the site's own assets; an interstitial is
# not. CLAUDE.md §8's structural secondary signal, and on this site the
# asset host is unambiguous — measured 40 to 130 references on every served
# capture and 0 on neither refusal page, which both DO carry it, so this is
# used only to tell a served page from a network error page (Chromium's own
# error document carries the site's hostname in its title and none of its
# assets).
ASSET_HOST_MARKERS = ("gstatic.com/finance", "www.gstatic.com/_/finance")

# --------------------------------------------------------------------------
# Payload extraction
# --------------------------------------------------------------------------

_AF_CALL_RE = re.compile(r"AF_initDataCallback\((\{.*?\});?\)\s*;", re.S)
_AF_KEY_RE = re.compile(r"key:\s*'([^']+)'")


def iter_payload_blobs(html: Optional[str]) -> Iterator[Tuple[str, Any]]:
    """Yield `(key, data)` for every `AF_initDataCallback` blob in `html`.

    The `data:` value is found by balancing brackets rather than by a regex,
    because the payload contains bracket characters inside strings the moment
    a company is called something like `Bath & Body Works [Inc]` — and a
    non-greedy regex silently truncates the blob at the first `]` that looks
    right, which loses rows from the END of a list where nothing errors.

    A blob that does not parse is skipped rather than raised on: the page
    carries blobs this repo has no interest in, and one of them failing to
    be JSON is not a reason to lose the ones that did parse.
    """
    if not html:
        return
    for m in _AF_CALL_RE.finditer(html):
        blob = m.group(1)
        key_m = _AF_KEY_RE.search(blob)
        if not key_m:
            continue
        start = blob.find("data:")
        if start < 0:
            continue
        try:
            open_at = blob.index("[", start)
        except ValueError:
            continue
        depth = 0
        end_at = -1
        in_str = False
        esc = False
        for i in range(open_at, len(blob)):
            ch = blob[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end_at = i
                    break
        if end_at < 0:
            continue
        try:
            yield key_m.group(1), json.loads(blob[open_at:end_at + 1])
        except (ValueError, TypeError):
            continue


def payload(html: Optional[str]) -> Dict[str, Any]:
    """The page's blobs as `{key: data}`."""
    return dict(iter_payload_blobs(html))


def _walk(node: Any) -> Iterator[list]:
    """Every list in `node`, outermost first."""
    if isinstance(node, list):
        yield node
        for child in node:
            yield from _walk(child)


# --------------------------------------------------------------------------
# The two instrument record shapes
# --------------------------------------------------------------------------
#
# Google publishes an instrument two ways, and they carry different columns.
# Both are identified by SHAPE, never by position in the page.
#
# COMPACT (>= 22 slots) — a row in a list, and the nested detail of a rich
# record. Slot 21 is the canonical symbol:
#
#   0  entity id ("/m/07zln7n")        11 [as-of epoch]
#   1  [ticker, exchange]              12 IANA timezone
#   2  display name                    13 utc offset, seconds
#   3  type flag: 0 stock, 1 index     14 entity id again
#   4  currency (null on an index)     16 [ext-hours last, change, pct]
#   5  [last, change, change_pct,...]  19 session hours
#   7  previous close                  21 "TICKER:EXCHANGE"
#   9  ISO-3166 country
#
# RICH (>= 19 slots) — the subject of a quote page, carrying the session's
# open/high/low, volume, market cap and industry that a list row does not:
#
#   2  open        12 currency          16 market cap (0 = absent)
#   4  day low     13 "TICKER:EXCHANGE" 17 volume     (0 = absent)
#   5  day high    14 display name      18 industry
#   6  last        15 previous close    19 [the COMPACT record]
#   8  change      10 change pct
#
# Slot 17 was very nearly documented as "employees" — 152,020 on Alphabet is
# a plausible headcount. It is volume: on the same capture the daily bars
# give a median daily volume of 23.3M against a 152,020 premarket snapshot,
# BMW reads 231,772 against a 1.35M median, and the S&P 500 reads 3.28
# BILLION, which is a share count and not a payroll. Measure the field, do
# not read the number and recognise it.

_MIN_COMPACT = 22
_MIN_RICH = 20

# Google publishes instruments in TWO compact shapes, not one, and the
# second was found only because the currency and crypto captures parsed to
# zero rows while the stock, index and futures ones parsed cleanly:
#
#   VENUE-TRADED   slot 1 is ["GOOGL", "NASDAQ"], slot 3 is 0 or 1,
#                  slot 4 is the currency, slot 12 is the IANA zone.
#   PAIR           slot 1 is null, slot 3 is 3, slot 4 is null, slot 12 is
#                  null, and slot 15 carries the legs:
#                  ["EUR","USD","Euro","United States Dollar",mid,mid,1]
#                  with a trailing 1 for a currency pair and 2 for crypto.
#
# Slot 21 is the canonical symbol in BOTH, which is why it is the anchor.
#
# The pair's own leg list is a better answer than the symbol-shape guess
# this module first shipped: `len(base) == 3 and len(quote) == 3` calls
# `BTC-USD` a currency pair on a bad day and has no opinion at all about a
# five-letter token. The payload states which it is; read that instead.
_PAIR_TYPE_FLAG = 3
_PAIR_KIND_BY_FLAG = {1: "currency", 2: "crypto"}


def _pair_legs(v: Sequence[Any]) -> Optional[list]:
    """The `["EUR","USD",...,1]` leg list of a pair record, or None."""
    if len(v) <= 15:
        return None
    legs = v[15]
    if (isinstance(legs, list) and len(legs) >= 2
            and isinstance(legs[0], str) and isinstance(legs[1], str)):
        return legs
    return None


def _is_venue_compact(v: Any) -> bool:
    return (
        isinstance(v[1], list)
        and len(v[1]) == 2
        and all(isinstance(x, str) for x in v[1])
        and isinstance(v[2], str)
    )


def _is_pair_compact(v: Any) -> bool:
    return (
        v[1] is None
        and v[3] == _PAIR_TYPE_FLAG
        and isinstance(v[2], str)
        and _pair_legs(v) is not None
    )


def _is_compact(v: Any) -> bool:
    if (not isinstance(v, list)
            or len(v) < _MIN_COMPACT
            or not isinstance(v[0], str)
            or not v[0].startswith("/")
            or not isinstance(v[5], list)
            or len(v[5]) < 3
            or not isinstance(v[5][0], (int, float))
            or isinstance(v[5][0], bool)
            or not isinstance(v[21], str)
            or not v[21]):
        return False
    return _is_venue_compact(v) or _is_pair_compact(v)


def _is_rich(v: Any) -> bool:
    """The per-instrument record a quote page carries.

    Identified by its own canonical symbol at slot 13, its display name at
    14, and a nested compact record at 19 — never by which `ds:` key it
    arrived under.
    """
    return (
        isinstance(v, list)
        and len(v) > _MIN_RICH
        and isinstance(v[13], str)
        and bool(v[13])
        and isinstance(v[14], str)
        and isinstance(v[19], list)
        and _is_compact(v[19])
    )


def _compact_records(html_or_payload: Any) -> List[list]:
    data = html_or_payload if isinstance(html_or_payload, dict) else payload(html_or_payload)
    out: List[list] = []
    seen: set = set()
    for node in _walk(list(data.values())):
        if _is_compact(node) and id(node) not in seen:
            seen.add(id(node))
            out.append(node)
    return out


def _rich_records(html_or_payload: Any) -> List[list]:
    data = html_or_payload if isinstance(html_or_payload, dict) else payload(html_or_payload)
    out: List[list] = []
    for node in _walk(list(data.values())):
        if _is_rich(node):
            out.append(node)
    return out


# --------------------------------------------------------------------------
# Scalars
# --------------------------------------------------------------------------

def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _nonzero(v: Any) -> Optional[float]:
    """A figure the site writes as 0 when it means "there is none".

    Measured 2026-09-22: `market_cap` is 0 on the index, currency, crypto and
    futures captures and `volume` is 0 on the currency and crypto ones — a
    sentinel, not a measurement. CLAUDE.md §21's "zero is not a rating",
    which cost a sibling every average its consumers computed.
    """
    n = _num(v)
    return None if n is None or n == 0 else n


def _epoch_iso(v: Any) -> Optional[str]:
    """`[1790065102]` -> `"2026-09-22T08:18:22+00:00"`."""
    from datetime import datetime, timezone as _tz
    secs = None
    if isinstance(v, list) and v and isinstance(v[0], (int, float)):
        secs = v[0]
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        secs = v
    if not secs:
        return None
    try:
        return datetime.fromtimestamp(float(secs), _tz.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def instrument_type(compact: Sequence[Any]) -> str:
    """"stock" | "index" | "currency" | "crypto" | "futures".

    Read from the payload's own flags, never from the url — a url's symbol
    is whatever the caller typed, and the caller can type one the site does
    not have.
    """
    legs = _pair_legs(compact) if compact[1] is None else None
    if legs is not None and compact[3] == _PAIR_TYPE_FLAG:
        flag = legs[-1] if isinstance(legs[-1], int) else None
        # An unknown flag is reported as a pair rather than guessed into one
        # of the two known kinds.
        return _PAIR_KIND_BY_FLAG.get(flag, "pair")
    exchange = compact[1][1] if isinstance(compact[1], list) and len(compact[1]) == 2 else ""
    if compact[3] == 1 or exchange.startswith("INDEX"):
        return "index"
    if any(exchange.startswith(v) for v in _FUTURES_VENUES):
        return "futures"
    return "stock"


# Venues whose symbols are futures contracts, matched as a PREFIX. An
# explicit list rather than a pattern, because a contract code is not
# distinguishable from a ticker by shape and an over-broad rule would
# relabel ordinary equities — but a prefix rather than an exact match,
# because the root page's futures strip carries `CME_EMINIS`, which an
# exact list missed: `ESW00:CME_EMINIS` and `NQW00:CME_EMINIS` were
# labelled "stock" until this was measured.
_FUTURES_VENUES = ("COMEX", "NYMEX", "CBOT", "CME", "ICE")


# --------------------------------------------------------------------------
# Row building
# --------------------------------------------------------------------------

def _row_from_compact(c: Sequence[Any], *, url: str, listing: Optional[str],
                      market: Optional[str], page: Optional[int],
                      position: Optional[int]) -> Quote:
    last = c[5] if isinstance(c[5], list) else []
    symbol = c[21]
    legs = _pair_legs(c) if c[1] is None else None
    is_pair = legs is not None and c[3] == _PAIR_TYPE_FLAG

    if is_pair:
        # A pair names no venue and no listing country, and the payload puts
        # null where a venue-traded instrument carries its currency and
        # timezone. The quote leg IS the currency the price is in — EUR-USD
        # is quoted in USD — and that is a read of the site's own leg list,
        # not an inference from the symbol's shape.
        ticker: Optional[str] = legs[0]
        exchange: Optional[str] = None
        currency: Optional[str] = legs[1]
        country: Optional[str] = None
        timezone: Optional[str] = None
        utc_offset: Optional[int] = None
        # Extended hours is a venue concept; a pair trades continuously.
        ext: Sequence[Any] = []
    else:
        ticker, exchange = (list(c[1]) + ["", ""])[:2]
        ticker = ticker or None
        exchange = exchange or None
        currency = c[4] if isinstance(c[4], str) else None
        country = c[9] if len(c) > 9 and isinstance(c[9], str) else None
        timezone = c[12] if len(c) > 12 and isinstance(c[12], str) else None
        utc_offset = c[13] if len(c) > 13 and isinstance(c[13], int) else None
        ext = c[16] if len(c) > 16 and isinstance(c[16], list) else []

    return Quote(
        url=url or quote_url(symbol),
        sku=symbol,
        title=c[2],
        price=_num(last[0] if len(last) > 0 else None),
        currency=currency,
        ticker=ticker,
        exchange=exchange,
        instrument_type=instrument_type(c),
        prev_close=_num(c[7] if len(c) > 7 else None),
        change=_num(last[1] if len(last) > 1 else None),
        change_pct=_num(last[2] if len(last) > 2 else None),
        country=country,
        timezone=timezone,
        utc_offset=utc_offset,
        quoted_at=_epoch_iso(c[11] if len(c) > 11 else None),
        after_hours_price=_num(ext[0] if len(ext) > 0 else None),
        after_hours_change=_num(ext[1] if len(ext) > 1 else None),
        after_hours_change_pct=_num(ext[2] if len(ext) > 2 else None),
        entity_id=c[0],
        listing=listing,
        market=market,
        price_source="af_list",
        page=page,
        position=position,
    )


def _row_from_rich(r: Sequence[Any], *, url: str, market: Optional[str],
                   page: Optional[int]) -> Quote:
    row = _row_from_compact(r[19], url=url, listing=None, market=market,
                            page=page, position=1)
    # The rich record's own columns, which a list row does not carry.
    row.open = _num(r[2])
    row.day_low = _num(r[4])
    row.day_high = _num(r[5])
    row.price = _num(r[6]) if _num(r[6]) is not None else row.price
    row.change = _num(r[8]) if _num(r[8]) is not None else row.change
    row.change_pct = _num(r[10]) if _num(r[10]) is not None else row.change_pct
    row.currency = r[12] if isinstance(r[12], str) else row.currency
    row.title = r[14] if isinstance(r[14], str) else row.title
    row.prev_close = _num(r[15]) if _num(r[15]) is not None else row.prev_close
    row.market_cap = _nonzero(r[16])
    vol = _nonzero(r[17])
    row.volume = int(vol) if vol is not None else None
    row.industry = r[18] if isinstance(r[18], str) else None
    row.price_source = "af_quote"
    return row


# --------------------------------------------------------------------------
# The three modes
# --------------------------------------------------------------------------

def _subject_record(data: Dict[str, Any], wanted: Optional[str]) -> Optional[list]:
    """The rich record for the instrument this url asked for.

    Picks the FRESHEST where a page carries more than one, and that is a
    determinism fix rather than a nicety. The 2026-09-22 Shell capture holds
    TWO rich records for SHEL:LON, taken 13 seconds apart — 3490.0 and
    3489.5 — because the page updates its quote while it is being served.
    Which one a walk reached first depended on which blobs were present, so
    a trimmed fixture and the capture it was cut from disagreed about the
    price, and two engines could disagree the same way.

    Freshest is the defensible choice and the one a reader would expect: the
    row then carries the last quote the page had, and `quoted_at` says when.
    Records with no timestamp sort last rather than being dropped.

    Returns None when the url names an instrument the page does not carry —
    which is load-bearing, because the page chrome carries 35 instrument
    records whatever the url asked for.
    """
    matches = [r for r in _rich_records(data)
               if not wanted or _symbols_match(r[13], wanted)]
    if not matches:
        return None
    if wanted is None and len(matches) > 1:
        # No url to match against and several candidates: refuse rather
        # than guess which instrument the caller meant.
        return None

    def freshness(rec):
        nested = rec[19]
        stamp = _epoch_iso(nested[11] if len(nested) > 11 else None)
        return stamp or ""

    return max(matches, key=freshness)


def parse_product_page(html: Optional[str], url: str = "", *,
                       market: Optional[str] = None,
                       page: Optional[int] = None) -> List[Quote]:
    """`--mode quote`: the one instrument this url names.

    Returns a list of 0 or 1 rows, and the 0 case is load-bearing. The page
    chrome carries the index and sector strips whatever the url asked for —
    35 instrument records on the Page Not Found capture — so this function
    matches the RICH record against the symbol the url asked for and returns
    nothing rather than the nearest thing it can find.
    """
    if not html:
        return []
    data = payload(html)
    chosen = _subject_record(data, symbol_from_url(url))
    if chosen is None:
        return []
    return [_row_from_rich(chosen, url=url or quote_url(chosen[13]),
                           market=market, page=page)]


# The root page's three mover lists, in the order the payload states them.
#
# The payload does NOT label them — it is three bare positional sub-lists
# inside one blob, with no enum, title or key to read. So the labels below
# are positional, and positional is exactly what this module refuses to do
# with `ds:N`. The difference is evidence, measured across six markets (US,
# DE, GB, JP, IN and the exit IP's own) on 2026-09-22:
#
#     sub-list 0    all change_pct > 0, sorted descending      6 of 6 markets
#     sub-list 1    all change_pct < 0, sorted by magnitude    6 of 6 markets
#     sub-list 2    neither, and the only list the rendered
#                   markup names ("Most active", once)
#
# `_movers_labels_hold()` re-checks that invariant on every parse and the
# engines downgrade the labels to positional names when it fails, so a
# reordering upstream costs a column's precision rather than silently
# mislabelling every row.
MOVER_LISTS = ("gainers", "losers", "most_active")

# The strips `--mode markets` reads, which are exactly the listings the
# retired `/finance/markets/*` urls used to serve one per page.
MARKET_STRIPS = ("index", "sector", "currency", "crypto", "futures")

# Which page kind each mode reads. One definition, read by the engines'
# argument validation, by page_flow's readiness map and by the suite — so a
# new mode cannot be wired into one of them and forgotten in the others.
QUOTE_PAGE_MODES = ("quote", "financials", "analysts", "chart")
MARKET_PAGE_MODES = ("markets", "movers", "earnings")
ALL_MODES = QUOTE_PAGE_MODES + MARKET_PAGE_MODES


def _movers_labels_hold(sublists: Sequence[Sequence[Sequence[Any]]]) -> bool:
    if len(sublists) < 2:
        return False
    gainers, losers = sublists[0], sublists[1]
    def pct(rec):
        return rec[5][2] if isinstance(rec[5], list) and len(rec[5]) > 2 else None
    g = [pct(r) for r in gainers if pct(r) is not None]
    l = [pct(r) for r in losers if pct(r) is not None]
    if not g or not l:
        return False
    return all(x > 0 for x in g) and all(x < 0 for x in l)


def _mover_sublists(data: Dict[str, Any]) -> List[List[list]]:
    """The movers blob: the one whose data is exactly three sub-lists, each
    holding instrument records.

    Found by shape, and the tie-break is the largest total — the root page
    carries other three-element lists, and the one that is three POPULATED
    lists of instruments is the movers strip. `gl=BR` returns a root page
    with no such blob at all, which is why an absent list is an empty result
    and never a failure.
    """
    best: List[List[list]] = []
    best_total = 0
    for value in data.values():
        if not isinstance(value, list) or len(value) != 3:
            continue
        subs = [_compact_records({"_": sub}) for sub in value]
        if not all(subs):
            continue
        total = sum(len(s) for s in subs)
        if total > best_total:
            best, best_total = subs, total
    return best


def parse_movers(html: Optional[str], url: str = "", *,
                 market: Optional[str] = None) -> List[Quote]:
    """`--mode movers`: the root page's gainers, losers and most-active.

    These are a PREVIEW, not a ranking. Google retired the standalone
    `/finance/markets/gainers` pages — all seven `/markets/*` paths now
    redirect to the root — and what the root publishes is 1 to 4 rows per
    list (measured across six markets on 2026-09-22), not a full top-N. A
    consumer wanting a complete ranking will not get one from this site, and
    saying so is cheaper than letting them infer a truncation bug.
    """
    data = payload(html)
    subs = _mover_sublists(data)
    if not subs:
        return []
    labelled = _movers_labels_hold(subs)
    rows: List[Quote] = []
    for idx, sub in enumerate(subs):
        name = MOVER_LISTS[idx] if labelled and idx < len(MOVER_LISTS) else f"movers_{idx}"
        for pos, rec in enumerate(sub, 1):
            rows.append(_row_from_compact(rec, url=quote_url(rec[21]),
                                          listing=name, market=market,
                                          page=1, position=pos))
    return rows


# The root page's SECTOR group, identified by the payload's own label.
#
# This was a ticker pattern first — `^(SIX|SX)` on a sector-index venue —
# and it was wrong on a real row: `SX5E:INDEXSTOXX` is the Euro Stoxx 50, a
# broad market index that lives in the INDICES blob, and the pattern
# relabelled it a sector on every US run. A regex over a ticker is a guess
# about what a name means; the payload states the answer.
#
# What it states: the sector blob's data opens with the literal string
# "sectors", followed by the group's entity ids and its own title ("Equity
# sectors"). Membership of that node is the signal — CLAUDE.md §17's rule
# that signals are ordered by how much they PROVE, not by how cheap they
# are.
_SECTOR_GROUP_MARKER = "sectors"


def _sector_record_ids(data: Dict[str, Any]) -> set:
    """`id()` of every compact record inside the payload's sector group."""
    ids: set = set()
    for node in _walk(list(data.values())):
        if (node and isinstance(node[0], str)
                and node[0] == _SECTOR_GROUP_MARKER):
            for rec in _walk(node):
                if _is_compact(rec):
                    ids.add(id(rec))
    return ids


def parse_markets(html: Optional[str], url: str = "", *,
                  market: Optional[str] = None) -> List[Quote]:
    """`--mode markets`: every strip the root page publishes except movers.

    Measured on the 2026-09-22 `gl=US` capture: 46 distinct instruments —
    19 broad indices, 11 sector indices, 5 currency pairs, 5 crypto pairs
    and 5 futures, plus the Euro Stoxx 50 among the indices. Those middle
    three groups are what the retired
    `/finance/markets/{currencies,cryptocurrencies}` urls used to serve, and
    an early version of this function dropped all fifteen of them by
    filtering for `instrument_type == "index"`. They are the most valuable
    rows on the page; the filter was the bug.

    The indices strip is published TWICE under two keys with identical
    content, so rows are deduped by symbol. Movers are excluded because they
    are `--mode movers`, and including them here would put the same symbol
    in a run twice under two different `listing` values.
    """
    data = payload(html)
    mover_ids = {id(r) for sub in _mover_sublists(data) for r in sub}
    sector_ids = _sector_record_ids(data)
    # The indices strip is published TWICE under two keys, and the two
    # copies are not byte-identical: they are the same instruments quoted
    # seconds apart. Keeping "the first one walked" therefore made the
    # output depend on walk order, which is how a trimmed fixture and the
    # capture it came from ended up disagreeing about a price while
    # reporting the same row count. Freshest wins, the same rule
    # `_subject_record` applies on a quote page.
    best: Dict[str, list] = {}
    for rec in _compact_records(data):
        if id(rec) in mover_ids:
            continue
        symbol = rec[21]
        prev = best.get(symbol)
        if prev is None or _record_stamp(rec) > _record_stamp(prev):
            best[symbol] = rec
    # Sorted by (strip, symbol) rather than left in payload order, and that
    # is OUR order rather than Google's — said plainly because a reader will
    # otherwise assume `position` reflects how the site ranks a strip.
    #
    # It cannot reflect that, because the site publishes its indices strip
    # TWICE and the two copies walk in different orders depending on which
    # blobs a document happens to carry. Left unsorted, the same 45 rows
    # came out sectors-first from one blob subset and indices-first from
    # another — same set, different `position` on every row, which is a diff
    # every night for nothing. Movers are deliberately NOT sorted this way:
    # their order within a list IS the ranking, and they arrive in one blob.
    ordered = sorted(best.values(),
                     key=lambda r: (MARKET_STRIPS.index(strip_of(r, sector_ids))
                                    if strip_of(r, sector_ids) in MARKET_STRIPS
                                    else len(MARKET_STRIPS), r[21]))
    rows: List[Quote] = []
    for rec in ordered:
        rows.append(_row_from_compact(rec, url=quote_url(rec[21]),
                                      listing=strip_of(rec, sector_ids),
                                      market=market, page=1,
                                      position=len(rows) + 1))
    return rows


def _record_stamp(rec: Sequence[Any]) -> str:
    """A compact record's as-of time, as a sortable string ("" if absent)."""
    return _epoch_iso(rec[11] if len(rec) > 11 else None) or ""


def strip_of(rec: Sequence[Any], sector_ids: Optional[set] = None) -> str:
    """Which of the root page's strips a record belongs to.

    Derived from the payload's own type flags, venue and group membership
    rather than from the record's position on the page, for the same reason
    nothing here anchors on `ds:N`.
    """
    kind = instrument_type(rec)
    if kind == "index":
        return "sector" if sector_ids and id(rec) in sector_ids else "index"
    return kind


# ---------------------------------------------------------------------------
# Mode C: financials, analysts, earnings, chart
# ---------------------------------------------------------------------------
#
# Everything below reads the SAME payload a quote page already carries — no
# extra fetch, no click, no XHR. The four "tabs" the site renders (Overview,
# Analysis, Earnings, Financials, Holdings) are rendered client-side out of
# data that arrived in the first response.

# Slot -> column for a reporting period's figure array. Seven of 106 slots,
# and the other 99 are deliberately unexposed: see `output_writer.Financial`
# for how these were established and why a plausible guess at the rest would
# be worse than nothing.
_FIN_SLOTS = {
    "revenue": 0,
    "net_income": 1,
    "net_profit_margin": 3,
    "eps": 9,
    "ebitda": 20,
    "effective_tax_rate": 21,
    "operating_expense": 38,
}
# The two that a period which has not happened yet still carries.
_FIN_ESTIMATE_SLOTS = {"revenue_estimate": 8, "eps_estimate": 10}
_FIN_CURRENCY_SLOT = 16
_FIN_PERIOD_END_SLOT = 17

# The two array widths the site publishes. The 18-slot form is a strict
# PREFIX of the 106-slot one — verified on Alphabet's 2024 Q2, where slot 0
# is revenue, 9 is EPS and 16/17 are the currency and period end, exactly as
# in a detailed quarter — so one slot map serves both and the narrow rows
# simply have fewer columns filled.
_FIN_WIDTHS = (18, 106)


def _iso_date(v: Any) -> Optional[str]:
    """`[2026, 6, 30]` -> `"2026-06-30"`."""
    if (isinstance(v, list) and len(v) == 3
            and all(isinstance(x, int) for x in v)):
        y, m, d = v
        if 1900 < y < 2200 and 1 <= m <= 12 and 1 <= d <= 31:
            return "%04d-%02d-%02d" % (y, m, d)
    return None


def _period_nodes(data: Dict[str, Any]) -> List[list]:
    """Every `[year, quarter, [figures...]]` a page carries.

    Found by shape: a plausible year, a quarter in range, and a figure array
    of one of the two published widths. Deduped by (year, quarter) keeping
    the WIDER array, because a page can carry the same quarter twice — once
    detailed and once in a summary strip — and the narrow copy would
    otherwise shadow the detailed one depending on walk order.
    """
    best: Dict[tuple, list] = {}
    for node in _walk(list(data.values())):
        if (len(node) >= 3 and isinstance(node[0], int)
                and 1990 < node[0] < 2200
                and isinstance(node[1], int) and 1 <= node[1] <= 4
                and isinstance(node[2], list) and len(node[2]) in _FIN_WIDTHS):
            key = (node[0], node[1])
            if key not in best or len(node[2]) > len(best[key][2]):
                best[key] = node
    return [best[k] for k in sorted(best, reverse=True)]


def parse_financials(html: Optional[str], url: str = "", *,
                     page: Optional[int] = None,
                     market: Optional[str] = None) -> List[Any]:
    """`--mode financials`: one row per reporting period.

    Measured on the 2026-09-22 Alphabet capture: 90 periods, quarterly back
    to 2004, of which the 8 most recent carry the full 106-figure array and
    the rest carry the 18-figure summary. That split is reported in the
    `detailed` column rather than smoothed over, so a consumer can tell a
    summary period from a parsing failure.
    """
    from output_writer import Financial
    if not html:
        return []
    data = payload(html)
    subject = _subject_record(data, symbol_from_url(url))
    if subject is None:
        return []
    symbol, name = subject[13], subject[14]

    rows: List[Any] = []
    for pos, node in enumerate(_period_nodes(data), 1):
        figures = node[2]

        def slot(i):
            v = figures[i] if i < len(figures) else None
            return _num(v)

        row = Financial(
            url=url or quote_url(symbol),
            sku=symbol,
            title=name,
            fiscal_year=node[0],
            fiscal_quarter=node[1],
            period_end=_iso_date(figures[_FIN_PERIOD_END_SLOT]
                                 if _FIN_PERIOD_END_SLOT < len(figures) else None),
            currency=(figures[_FIN_CURRENCY_SLOT]
                      if _FIN_CURRENCY_SLOT < len(figures)
                      and isinstance(figures[_FIN_CURRENCY_SLOT], str) else None),
            detailed=len(figures) == max(_FIN_WIDTHS),
            page=page,
            position=pos,
        )
        for field_name, idx in _FIN_SLOTS.items():
            setattr(row, field_name, slot(idx))
        for field_name, idx in _FIN_ESTIMATE_SLOTS.items():
            setattr(row, field_name, slot(idx))
        # A period with no figure at all is a structural match on noise, not
        # a reporting period. Dropped rather than written as a row of nulls.
        if any(getattr(row, f) is not None for f in _FIN_SLOTS):
            rows.append(row)
    return rows


# The consensus header: [name, currency, low, high, mean, upside, total,
# verdict, buy, SELL, HOLD, ...]. Slots 9 and 10 are the surprising pair —
# see `output_writer.AnalystRating` for the live reading that settled them.
_CONSENSUS_MIN = 11
_BUY_VERDICTS = ("StrongBuy", "Buy")
_SELL_VERDICTS = ("StrongSell", "Sell")


def _consensus_record(data: Dict[str, Any]) -> Optional[list]:
    for node in _walk(list(data.values())):
        if (len(node) >= _CONSENSUS_MIN
                and isinstance(node[0], str) and node[0]
                and isinstance(node[1], str) and len(node[1]) == 3
                and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                        for x in node[2:7])
                and isinstance(node[7], str) and node[7]
                and all(isinstance(x, int) for x in node[8:11])):
            return node
    return None


def rating_breakdown(rec: Sequence[Any]) -> Dict[str, Optional[int]]:
    """Buy / hold / sell, or three Nones if the payload disagrees with itself.

    The slot order here is counter-intuitive and was read off the rendered
    page rather than assumed, so it is also CHECKED rather than trusted. Two
    invariants, both cheap:

      * the three counts must sum to the stated total;
      * a buy-leaning verdict must not carry more sells than buys (and the
        mirror for a sell-leaning one).

    When either fails, the counts are nulled and the total kept. That is a
    downgrade rather than a mislabel — the same answer this module gives
    when the movers lists stop looking like gainers and losers — because a
    swapped hold/sell pair is exactly the kind of error that reads as
    plausible for years.
    """
    total = rec[6] if isinstance(rec[6], int) else None
    buy, sell, hold = rec[8], rec[9], rec[10]
    verdict = rec[7]
    null = {"buy_count": None, "hold_count": None, "sell_count": None}
    if total is None or buy + sell + hold != total:
        return null
    if verdict in _BUY_VERDICTS and sell > buy:
        return null
    if verdict in _SELL_VERDICTS and buy > sell:
        return null
    return {"buy_count": buy, "hold_count": hold, "sell_count": sell}


# An analyst action: [id, analyst, firm, rating, "MM/DD/YYYY", url, image,
# ...tipranks stats..., headline, byline, headline, target, ...].
_ACTION_MIN = 20
_ACTION_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def _action_records(data: Dict[str, Any]) -> List[list]:
    out = []
    for node in _walk(list(data.values())):
        if (len(node) >= _ACTION_MIN
                and isinstance(node[0], str) and len(node[0]) >= 16
                and isinstance(node[1], str) and isinstance(node[2], str)
                and isinstance(node[3], str)
                and isinstance(node[4], str)
                and _ACTION_DATE_RE.match(node[4])):
            out.append(node)
    return out


def parse_analysts(html: Optional[str], url: str = "", *,
                   page: Optional[int] = None,
                   market: Optional[str] = None) -> List[Any]:
    """`--mode analysts`: the consensus, and every published action.

    One row per action with the consensus repeated on each — see
    `output_writer.AnalystRating` for why denormalised. Where the site
    publishes a consensus and no actions, one row carries the consensus
    alone rather than nothing.
    """
    from output_writer import AnalystRating
    if not html:
        return []
    data = payload(html)
    subject = _subject_record(data, symbol_from_url(url))
    if subject is None:
        return []
    symbol, name = subject[13], subject[14]

    rec = _consensus_record(data)
    common: Dict[str, Any] = {}
    if rec is not None:
        common = {
            "consensus": rec[7],
            "analysts_total": rec[6] if isinstance(rec[6], int) else None,
            "target_low": _num(rec[2]),
            "target_high": _num(rec[3]),
            "target_mean": _num(rec[4]),
            "target_upside_pct": _num(rec[5]),
            "target_currency": rec[1],
        }
        common.update(rating_breakdown(rec))

    rows: List[Any] = []
    for pos, act in enumerate(_action_records(data), 1):
        m = _ACTION_DATE_RE.match(act[4])
        rows.append(AnalystRating(
            url=url or quote_url(symbol), sku=symbol, title=name,
            analyst=act[1] or None,
            firm=act[2] or None,
            action=act[3] or None,
            action_date="%s-%s-%s" % (m.group(3), m.group(1), m.group(2)),
            price_target=_num(_first_target(act)),
            headline=_first_str_after(act, 16),
            headline_url=act[5] if isinstance(act[5], str)
            and act[5].startswith("http") else None,
            page=page, position=pos, **common))
    if not rows and common:
        rows.append(AnalystRating(url=url or quote_url(symbol), sku=symbol,
                                  title=name, page=page, position=1, **common))
    return rows


def _first_target(act: Sequence[Any]) -> Optional[float]:
    """The price target in an action record.

    Taken as the first number that sits immediately before the record's own
    currency code, because the record repeats the target two or three times
    with the currency after each — reading a fixed slot would work on the
    capture it was written against and break on the next shape.
    """
    for i, v in enumerate(act):
        if (isinstance(v, str) and len(v) == 3 and v.isupper()
                and i >= 2 and isinstance(act[i - 2], (int, float))
                and not isinstance(act[i - 2], bool)):
            return act[i - 2]
    return None


def _first_str_after(act: Sequence[Any], start: int) -> Optional[str]:
    for v in act[start:]:
        if isinstance(v, str) and len(v) > 20 and " " in v:
            return v
    return None


def parse_earnings(html: Optional[str], url: str = "", *,
                   market: Optional[str] = None,
                   page: Optional[int] = None) -> List[Any]:
    """`--mode earnings`: the market page's upcoming announcements.

    Read from the market page rather than a quote page, so this answers "who
    reports this week" rather than "when does X report". 5 events on the
    2026-09-22 gl=US capture. The calendar is geo-selected like every other
    list on that page, so `market` is recorded on each row.
    """
    from output_writer import EarningsEvent
    if not html:
        return []
    rows: List[Any] = []
    for pos, node in enumerate(_earnings_nodes(payload(html)), 1):
        ticker, exchange = node[0]
        detail = node[3] if len(node) > 3 and isinstance(node[3], list) else []
        figures = next((x for x in detail
                        if isinstance(x, list) and len(x) in _FIN_WIDTHS), [])

        def slot(i):
            v = figures[i] if i < len(figures) else None
            return _num(v)

        rows.append(EarningsEvent(
            url=quote_url("%s:%s" % (ticker, exchange)),
            sku="%s:%s" % (ticker, exchange),
            title=detail[2] if len(detail) > 2 and isinstance(detail[2], str) else None,
            event_title=node[2],
            event_date=_iso_date(node[1]),
            event_at=_epoch_iso(next((x for x in detail
                                      if isinstance(x, list) and len(x) == 1
                                      and isinstance(x[0], int)
                                      and x[0] > 1_000_000_000), None)),
            fiscal_year=detail[3] if len(detail) > 3 and isinstance(detail[3], int) else None,
            fiscal_quarter=detail[4] if len(detail) > 4 and isinstance(detail[4], int) else None,
            period_end=_iso_date(figures[_FIN_PERIOD_END_SLOT]
                                 if _FIN_PERIOD_END_SLOT < len(figures) else None),
            currency=(figures[_FIN_CURRENCY_SLOT]
                      if _FIN_CURRENCY_SLOT < len(figures)
                      and isinstance(figures[_FIN_CURRENCY_SLOT], str) else None),
            revenue_estimate=slot(_FIN_ESTIMATE_SLOTS["revenue_estimate"]),
            eps_estimate=slot(_FIN_ESTIMATE_SLOTS["eps_estimate"]),
            market=market, page=page, position=pos))
    return rows


def _earnings_nodes(data: Dict[str, Any]) -> List[list]:
    out = []
    for node in _walk(list(data.values())):
        if (len(node) >= 3
                and isinstance(node[0], list) and len(node[0]) == 2
                and all(isinstance(x, str) and x for x in node[0])
                and _iso_date(node[1])
                and isinstance(node[2], str) and "Earnings" in node[2]):
            out.append(node)
    return out


# An OHLCV bar: [open, close, high, low, "ISO timestamp", volume]. Close is
# SECOND — verified against the quote record's own open/high/low/last on two
# instruments rather than assumed; see `output_writer.ChartPoint`.
_BAR_LEN = 6
_BAR_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _is_bar(v: Any) -> bool:
    return (isinstance(v, list) and len(v) == _BAR_LEN
            and isinstance(v[4], str) and bool(_BAR_TS_RE.match(v[4]))
            and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                    for x in (v[0], v[1], v[2], v[3], v[5])))


def _bar_series(data: Dict[str, Any]) -> List[Tuple[str, List[list]]]:
    """Every contiguous OHLCV SERIES, labelled "5m" or "1d".

    Labelled per SERIES rather than per bar, and that is a bug fix rather
    than a refinement. The first version grouped bars by DATE and called a
    date holding many bars intraday — right for every bar but one: the daily
    bar for the CURRENT session carries the same timestamp as that session's
    last five-minute bar (`2026-09-21T16:00:00-04:00` on the Alphabet
    capture). Grouped by date it joined the intraday run and was labelled
    "5m" while carrying the whole session's open, high, low and volume — a
    full-day bar wearing a five-minute label.

    It was found because the three engines stopped agreeing: two series
    holding a bar with the same timestamp ordered differently under
    different walk orders, so one engine put a different row at position 97.
    That is the family's "all three engines must produce identical rows"
    rule paying for itself — nothing about the output looked wrong on its
    own.

    A series is a list whose elements are ALL bars, which is how the payload
    stores them. Its interval is the median gap between consecutive bars:
    under a day is intraday, a day or more is daily. Derived from the data
    rather than from which blob it arrived in, because blob identity is the
    one thing this module refuses to depend on.
    """
    from datetime import datetime
    out: List[Tuple[str, List[list]]] = []
    seen: set = set()
    for node in _walk(list(data.values())):
        if len(node) < 2 or not all(_is_bar(x) for x in node):
            continue
        if id(node) in seen:
            continue
        seen.add(id(node))
        gaps = []
        for a, b in zip(node, node[1:]):
            try:
                gaps.append(abs((datetime.fromisoformat(b[4])
                                 - datetime.fromisoformat(a[4])).total_seconds()))
            except ValueError:
                continue
        if not gaps:
            continue
        gaps.sort()
        median = gaps[len(gaps) // 2]
        out.append(("1d" if median >= 23 * 3600 else "5m", list(node)))
    return out


def parse_chart(html: Optional[str], url: str = "", *,
                page: Optional[int] = None,
                market: Optional[str] = None) -> List[Any]:
    """`--mode chart`: every OHLCV bar the page already carries.

    Both intervals, in one run, with no extra fetch: the latest session at
    five-minute resolution and about a month of daily bars. Measured on the
    2026-09-22 Alphabet capture: 99 bars, of which 79 are intraday (one
    full 09:30-16:00 session) and 20 daily.

    `--window` does not deepen this — every value from 5D to MAX returned
    the same daily series, because the deeper history is fetched
    client-side. That is stated here and in the README rather than left for
    a reader to discover as a broken flag.
    """
    from output_writer import ChartPoint
    if not html:
        return []
    data = payload(html)
    subject = _subject_record(data, symbol_from_url(url))
    if subject is None:
        return []
    symbol, name = subject[13], subject[14]
    currency = subject[12] if isinstance(subject[12], str) else None

    rows: List[Any] = []
    pos = 0
    # Sorted so a run's output is stable whatever order the payload happened
    # to walk in. Two series can hold a bar with the SAME timestamp — the
    # current session's daily bar and its last intraday bar — so sorting on
    # the timestamp alone is not enough, and an unstable order here is
    # exactly what made the mislabelling above visible.
    for interval, series in sorted(_bar_series(data),
                                   key=lambda s: (s[0], s[1][0][4])):
        for b in series:
            pos += 1
            rows.append(ChartPoint(
                url=url or quote_url(symbol), sku=symbol, title=name,
                interval=interval, ts=b[4],
                open=_num(b[0]), close=_num(b[1]),
                high=_num(b[2]), low=_num(b[3]),
                volume=int(b[5]) if isinstance(b[5], (int, float)) else None,
                currency=currency, page=page, position=pos))
    return rows


def parse_products(html: Optional[str], url: str = "", page: Optional[int] = None,
                   *, mode: str = "quote", market: Optional[str] = None,
                   **_ignored: Any) -> List[Quote]:
    """The family entry point: rows for `html`, whichever mode is running."""
    if mode == "markets":
        return parse_markets(html, url, market=market)
    if mode == "movers":
        return parse_movers(html, url, market=market)
    if mode == "financials":
        return parse_financials(html, url, page=page, market=market)
    if mode == "analysts":
        return parse_analysts(html, url, page=page, market=market)
    if mode == "earnings":
        return parse_earnings(html, url, market=market, page=page)
    if mode == "chart":
        return parse_chart(html, url, page=page, market=market)
    return parse_product_page(html, url, market=market, page=page)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def _unescaped_head(html: Optional[str], limit: int = 200_000) -> str:
    """A bounded, entity-normalised prefix.

    CLAUDE.md §20: an edge can entity-escape the punctuation in its own
    refusal page, so a literal marker matches the browser's serialisation and
    silently misses the HTTP client's. Bounded because unescaping 1.4 MB on
    every fetch buys nothing and risks a company name deep in a payload
    reading as a marker.
    """
    if not html:
        return ""
    return _html.unescape(html[:limit])


def detect_block_marker(html: Optional[str]) -> Optional[str]:
    """The first refusal marker present, or None."""
    head = _unescaped_head(html)
    for marker in BOT_CHALLENGE_MARKERS:
        if marker in head:
            return marker
    return None


def detect_bot_challenge(html: Optional[str]) -> Optional[str]:
    """Family alias for `detect_block_marker`."""
    return detect_block_marker(html)


def is_not_found(html: Optional[str]) -> bool:
    """Does the site say it has no such instrument?

    Scanned over the WHOLE document, not the bounded prefix the challenge
    markers use, and that is measured rather than cautious: on the
    2026-09-22 capture the marker sits at byte 930,698 of a 987,996-byte
    page. A 200 KB bound — which is ample for a refusal page, those being a
    few KB — missed it completely and the classifier called a Page Not Found
    "empty", which is the one answer that sends a reader to check their
    symbol list instead of reading the site's own sentence.

    Scanning it all is safe here where it would not be for a URL-shaped
    marker: this is a literal `title=` attribute with no punctuation for an
    edge to entity-escape, so it needs no unescaping, and `in` over 1 MB is
    a memchr.
    """
    if not html:
        return False
    return any(m in html for m in NOT_FOUND_MARKERS)


def is_unsupported_client(html: Optional[str]) -> bool:
    """True on the page a non-browser User-Agent is redirected to.

    This is not a block and must not be reported as one: no address was
    refused and no proxy would help. It means the CLIENT was refused, and
    the fix is a browser User-Agent — which every engine in this repo sets,
    so seeing this state at all points at a misconfiguration rather than at
    the site.
    """
    if not html:
        return False
    return any(m in html for m in UNSUPPORTED_MARKERS)


def is_region_unavailable(html: Optional[str]) -> bool:
    """Does the page say Google Finance is not served in this country?

    Scanned over the whole document like the other state markers — the
    sentence sits inside Google's error page, which is small, but bounding
    it would be a guess about a page nobody here has captured.
    """
    if not html:
        return False
    return any(m in html for m in REGION_MARKERS)


def served_by_google(html: Optional[str]) -> bool:
    """Was this built out of Google Finance's own assets?

    The positive-asset check CLAUDE.md §8 and §18 both arrive at. Its job
    here is Chromium's own network-error page, which carries the site's
    hostname in its `<title>` — so a title check calls it a real page — and
    none of the site's assets.
    """
    if not html:
        return False
    return sum(html.count(m) for m in ASSET_HOST_MARKERS) >= 2


def unsupported_reason(url: str) -> Optional[str]:
    """Why this repo refuses `url`, in the site's own terms, or None."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "not an absolute URL"
    if host not in HOSTS:
        return (f"{host} is not a Google Finance host — this repo reads "
                f"www.google.com/finance only")
    path = urlparse(url).path
    if not path.startswith("/finance"):
        return f"{path} is not a /finance path"
    return None


def is_supported_host(url: str) -> bool:
    return unsupported_reason(url) is None


def site_host(url: str = "") -> str:
    return (urlparse(url).hostname or SITE_HOST).lower()


def detect_page_state(html: Optional[str], status: Optional[int] = None,
                      url: str = "", mode: str = "quote") -> str:
    """One of: content | not_found | unsupported_client | blocked | empty |
    unknown.

    ORDER IS THE WHOLE POINT, and it is CLAUDE.md §17's classification-order
    trap taken seriously: the unambiguous positive signals are read before
    any threshold or count.

      1. `unsupported_client` — the site says so in its own canonical link.
      2. `not_found` — the site says so in its own words. Read BEFORE rows
         are counted, because a Not Found page parses to 35 instrument
         records of page chrome.
      3. `blocked` — a Google refusal marker, none of which appears on any
         served page.
      4. `content` / `empty` — only now does a count decide anything.

    `status` is taken where an engine can supply one. Selenium cannot, and
    this function is correct without it: every state above is settled by the
    body, because Google answers a refused client with HTTP 200 and a
    different page rather than with a status.
    """
    if not html:
        return "unknown"
    if is_unsupported_client(html):
        return "unsupported_client"
    if is_not_found(html):
        return "not_found"
    # Before the generic status rules below, because a regional refusal
    # arrives AS a 403 and "blocked" would send the reader to rotate an
    # address within a country that is refused wholesale.
    if is_region_unavailable(html):
        return "region_unavailable"
    if detect_block_marker(html):
        return "blocked"
    if status is not None and status >= 500:
        return "throttled"
    if status is not None and status in (401, 403, 429):
        return "blocked"
    if not served_by_google(html):
        return "unknown"
    rows = parse_products(html, url, mode=mode)
    return "content" if rows else "empty"


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

_SYMBOL_IN_URL_RE = re.compile(r"/finance/(?:beta/)?quote/([^/?#]+)")


def symbol_from_url(url: Optional[str]) -> Optional[str]:
    """`.../quote/GOOGL:NASDAQ?window=1Y` -> `GOOGL:NASDAQ`."""
    if not url:
        return None
    m = _SYMBOL_IN_URL_RE.search(url)
    if not m:
        return None
    from urllib.parse import unquote
    return unquote(m.group(1)) or None


def _symbols_match(payload_symbol: str, url_symbol: str) -> bool:
    """Is the record's symbol the one the url asked for?

    Case-insensitive, because the site accepts `googl:nasdaq` and answers
    with `GOOGL:NASDAQ`. Deliberately NOT a prefix or substring test: `BMW`
    is a prefix of nothing useful, but a substring rule would let a page
    about `META:NASDAQ` satisfy a request for `ETA:NASDAQ`.
    """
    return payload_symbol.strip().upper() == url_symbol.strip().upper()


def quote_url(symbol: str, market: Optional[str] = None,
              language: Optional[str] = None) -> str:
    """The canonical URL for a symbol, in the `/finance/beta/` form."""
    from urllib.parse import quote as _q
    url = f"https://{SITE_HOST}{BETA_PREFIX}/quote/{_q(symbol, safe=':.-')}"
    return with_market(url, market, language)


def markets_url(market: Optional[str] = None,
                language: Optional[str] = None) -> str:
    """The root page, which is every list this repo reads."""
    return with_market(f"https://{SITE_HOST}{BETA_PREFIX}/", market, language)


def with_market(url: str, market: Optional[str] = None,
                language: Optional[str] = None) -> str:
    """Set `gl` (and optionally `hl`) on `url`, replacing rather than
    duplicating, and preserving whatever else is already there."""
    if not market and not language:
        return url
    parts = urlparse(url)
    q = parse_qs(parts.query, keep_blank_values=True)
    if market:
        q[MARKET_PARAM] = [market.upper()]
    if language:
        q[LANGUAGE_PARAM] = [language]
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


def market_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    v = parse_qs(urlparse(url).query).get(MARKET_PARAM)
    return v[0].upper() if v else None


def listing_kind(url: str) -> str:
    """"quote" for an instrument url, "markets" for the root."""
    return "quote" if symbol_from_url(url) else "markets"


def canonical_url(url: str) -> str:
    """Rewrite a legacy `/finance/...` url to the `/finance/beta/...` the
    site redirects it to, saving a round trip and making two runs of the
    same instrument produce the same `url` column."""
    parts = urlparse(url)
    path = parts.path
    if path.startswith("/finance/") and not path.startswith("/finance/beta"):
        path = "/finance/beta/" + path[len("/finance/"):]
    elif path in ("/finance", "/finance/"):
        path = BETA_PREFIX + "/"
    return urlunparse(parts._replace(path=path))


# --------------------------------------------------------------------------
# Pagination — there is none, and that is the contract
# --------------------------------------------------------------------------

def paginates_by_url(url: str = "", html: Optional[str] = None) -> bool:
    """False, always, and deliberately not a stub.

    CLAUDE.md §7 says pagination must never depend solely on selectors and
    §18 adds that one page kind may have no per-page addresses at all. Google
    Finance is the case where NO page kind has them: a quote page is one
    instrument, and the root page's lists are whatever it publishes, with the
    seven former `/markets/*` urls now redirecting to it.

    Answering False here is what makes the engines refuse `--pages > 1` WITH
    A REASON instead of re-fetching the root page three times, finding no new
    symbol, and reporting a complete three-page run that read one page.
    """
    return False


def no_pagination_reason(url: str = "") -> str:
    return ("Google Finance does not paginate: a quote page is one instrument "
            "and the root page carries its lists in full. Use --symbols to "
            "read more than one instrument.")


PAGE_CAP: Optional[int] = 1


def pages_beyond_cap(requested: int, url: str = "",
                     html: Optional[str] = None) -> int:
    """How many of `requested` pages cannot exist. All but the first."""
    return max(0, int(requested or 1) - 1)


def capped_by_site(html: Optional[str] = None) -> bool:
    """True: every mode reads exactly what one page publishes."""
    return True


def reachable_max(html: Optional[str] = None) -> int:
    return 1


def total_results(html: Optional[str] = None, mode: str = "quote") -> Optional[int]:
    """How many rows the page holds, by the site's own reckoning.

    There is no stated total to read — unlike a search listing, the payload
    publishes the rows and nothing about a larger set they are drawn from —
    so this counts what was published. `capped_by_site` is True and
    `reachable_max` is 1, so a consumer reading the sidecar can tell this is
    a complete read of a small set rather than a truncated read of a big one.
    """
    if html is None:
        return None
    return len(parse_products(html, mode=mode))


def page_url(url: str, page: int) -> str:
    """Page 1's url, whatever `page` says.

    Not a silent no-op: `paginates_by_url()` is False and the engines check
    it before ever calling this, so reaching here with `page > 1` is a
    programming error in an engine rather than a site quirk. It returns the
    unchanged url rather than raising, because the family's page loop calls
    it defensively, and a check in the suite pins the refusal instead.
    """
    return canonical_url(url)


def search_header(html: Optional[str] = None) -> Optional[str]:
    """The site states no result header on any page kind here."""
    return None


def concurrency_limit(url: str = "") -> Optional[int]:
    """1 — but per PAGE, not per symbol.

    There is only ever one page per unit of work, so handing pages to
    workers is meaningless. `--mode quote` over many symbols IS
    parallelisable, and the engines do that over the symbol list instead.
    """
    return 1


def market_metadata(html: Optional[str], url: str = "") -> Dict[str, Any]:
    """Run metadata for the sidecar.

    `market` is load-bearing rather than decorative: the root page's lists
    are geo-selected, so two runs under different `gl` values are different
    SAMPLES and not a change in the data. `diff_runs.py` refuses to compare
    across it for the same reason CLAUDE.md §21 makes a sibling refuse to
    compare two sort orders.
    """
    return {
        "market": market_from_url(url),
        "listing_kind": listing_kind(url),
        "paginates": paginates_by_url(url),
        "symbol": symbol_from_url(url),
    }


def currency_from_page(html: Optional[str], url: str = "") -> Optional[str]:
    """The currency the page states, or None — never a default.

    Every row carries its own `currency` straight out of its own record, so
    unlike the sibling that had to thread a one-per-run currency forward from
    page 1, nothing here depends on this. It exists for the engines' sidecar
    and answers only when the page's rows agree.
    """
    rows = parse_products(html, url, mode=listing_kind(url))
    seen = {r.currency for r in rows if r.currency}
    return seen.pop() if len(seen) == 1 else None
