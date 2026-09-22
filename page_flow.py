"""page_flow.py — what to do with the page Google Finance just gave us.

Google Finance answers a request five ways and four of them want a different
response, which is why this module exists rather than the same triage being
written three times inside three engines and drifting apart (CLAUDE.md §1):

    content             the payload is there and holds the instrument(s)
                        this url asked for
    not_found           the site's own "Page Not Found". A DEFINITE answer
                        about a symbol, not a failure to fetch — retrying it
                        or rotating the exit changes nothing
    unsupported_client  the page a non-browser User-Agent is redirected to.
                        The CLIENT was refused, not the address
    blocked             Google's `/sorry/index` interstitial
    throttled           an HTTP 5xx. Wants a WAIT at the SAME exit, which is
                        what separates it from `blocked`

The two that cost the most to get wrong are the first two, and they are
distinguished by a marker rather than by a row count, deliberately:

    a Page Not Found page parses to 35 instrument records.

The page chrome carries the index and sector strips whatever the url asked
for. So "did we get rows?" answers YES on a page holding no such instrument,
and a classifier that counted first would report a successful quote fetch
full of furniture. `product_parser.detect_page_state` reads the site's own
sentence before it counts anything — CLAUDE.md §17's classification-order
trap, where a threshold was consulted ahead of an unambiguous marker.

The policy lives in `STATE_POLICY` as DATA, so an engine cannot quietly
disagree with its twins about whether a page is worth retrying or worth
paying for.

Everything here is pure or driven through small callables, so each engine
passes its own driver's primitives and keeps its browser plumbing to itself:

    count(selector) -> int          how many elements match
    content() -> Optional[str]      current HTML, None if unavailable
    sleep(ms) -> None               wait

No JavaScript crosses that boundary in either direction (§1): Selenium's
`execute_script` takes a function BODY with an explicit `return` where
Playwright and pyppeteer take `() => expr`, so the shared module names the
OPERATION and the engine spells it in its own driver's dialect.

THIS MODULE HAS NO SCROLL LOOP, AND NO PAGINATION EITHER
--------------------------------------------------------
Both measured rather than assumed, on 2026-09-22.

No scroll: a quote page carries its whole instrument record, and the root
page carries all 46 of its rows, in the FIRST response — server-rendered
into `AF_initDataCallback` blobs with no JavaScript run at all. Proof rather
than belief: the same url fetched by plain HTTP with a Chrome User-Agent (no
JS, no scroll, no browser) and by headless Chromium parse to the same rows.
A scroll here would be latency bought for nothing.

No pagination: the seven `/finance/markets/*` urls that used to serve
gainers, losers, most-active, currencies, cryptocurrencies, climate leaders
and indexes ALL redirect to the `/finance/beta/` root now — checked, seven
paths, seven redirects — and the root carries its lists in one payload. A
quote page is one instrument. So there is no page 2 anywhere on this site,
`pagination_is_addressable` answers False for every url, and the engines
refuse `--pages > 1` with that reason instead of fetching the same page
three times, finding no new symbol, and reporting a complete three-page run
that read one page.

The unit of work here is the SYMBOL. `--symbols A,B,C` is what the engines
iterate, and that IS parallelisable — see `concurrency_limit`.

And one thing NOT to do, which is the same trap a sibling paid for: never
wait on `networkidle`. Google Finance streams quote updates and fires
telemetry continuously, so a `networkidle` wait runs to its full timeout on
a page that was complete in under two seconds. Every engine here waits for
`domcontentloaded`.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, List, Optional, Union
from urllib.parse import urljoin, urlparse, urlunparse

from product_parser import (PAGE_CAP, canonical_url, capped_by_site,
                            detect_block_marker, detect_bot_challenge,
                            detect_page_state, is_not_found,
                            is_unsupported_client, listing_kind,
                            market_from_url, market_metadata,
                            no_pagination_reason, pages_beyond_cap,
                            paginates_by_url, reachable_max, served_by_google,
                            symbol_from_url, total_pages, total_results,
                            unsupported_reason)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
# What an engine waits for before deciding a page is worth reading.
#
# Both selectors are DOM anchors and neither is where data comes from — the
# rows are parsed out of the payload, which is present in the first response.
# They exist so that a browser engine does not read a half-built document and
# report `empty` for a page that was still attaching its own markup.

# One selector for both page kinds: an in-page link to an instrument.
#
# The href is matched as `quote/` rather than `/finance/quote/`, and that is
# measured rather than sloppy. Google Finance renders its anchors RELATIVE —
# in the live DOM they are `./quote/GOOGL:NASDAQ`, not absolute — so an
# absolute pattern matched **0 elements on both page kinds** while the page
# was fully painted, and the first live run spent its whole 20-second
# readiness timeout on every fetch before parsing the rows it already had.
#
# Counted in the live DOM on 2026-09-22, immediately after
# `domcontentloaded`:
#
#     selector                      quote page   root page
#     main a[href*="quote/"]              16          63
#     a[href*="/finance/quote/"]           0           0
#     [data-entityid], [role="main"]       0           0
#
# The lesson is the sibling's, in a new costume: measure a selector against
# the live DOM, not against the served bytes, because the served bytes of
# this site carry ONE anchor on a page whose DOM ends up with 63.
READY_SELECTOR_QUOTE = 'main a[href*="quote/"]'
READY_SELECTOR_MARKETS = 'main a[href*="quote/"]'

# Kept under the family's names so the engines' shared code reads the same
# here as in its siblings.
READY_SELECTOR_LISTING = READY_SELECTOR_MARKETS
READY_SELECTOR_PRODUCT = READY_SELECTOR_QUOTE

# How many matches mean "rendered".
#
# Must be > 1 on the list page (§5): waiting for a single match resolves on
# the site's own navigation chrome long before the strips paint. Measured on
# the 2026-09-22 captures: a served root page carries 40+ quote links and the
# unsupported-browser page carries 0.
MIN_CARD_MATCHES = 8

# A quote page is ONE instrument, so its floor is 1 and that is not a
# weakened version of the rule above — there is nothing else to wait for.
MIN_CARD_MATCHES_QUOTE = 1

CONTENT_TIMEOUT_MS = 20_000

# How many rows a "page" holds. There is no fixed page size on this site —
# a quote page is one instrument and the root page publishes 45 to 46 rows
# depending on the market — so this is the root page's measured size, used
# only for the engines' progress arithmetic and never to infer a total.
PAGE_SIZE = 46

# The instrument modes all read a quote page and the list modes all read the
# market page, so readiness is a property of the PAGE KIND rather than of
# the mode. Spelled out per mode anyway: a map is cheaper to read than a
# rule, and an engine cannot disagree with its twins about a lookup.
_QUOTE_PAGE_MODES = ("quote", "financials", "analysts", "chart")
_MARKET_PAGE_MODES = ("markets", "movers", "earnings")

_READY = {m: READY_SELECTOR_QUOTE for m in _QUOTE_PAGE_MODES}
_READY.update({m: READY_SELECTOR_MARKETS for m in _MARKET_PAGE_MODES})
_MIN = {m: MIN_CARD_MATCHES_QUOTE for m in _QUOTE_PAGE_MODES}
_MIN.update({m: MIN_CARD_MATCHES for m in _MARKET_PAGE_MODES})


def ready_selector(mode: str = "quote") -> str:
    return _READY.get(mode, READY_SELECTOR_QUOTE)


def min_matches(mode: str = "quote", expected: Optional[int] = None) -> int:
    """How many matches to wait for.

    `expected` lets a caller lower the floor when the page itself says it
    holds fewer rows than the default — the `found <= threshold` trap a
    sibling shipped, where a query with fewer results than the readiness
    floor waited out its whole timeout on a fully painted page.
    """
    floor = _MIN.get(mode, MIN_CARD_MATCHES_QUOTE)
    if expected is not None and expected > 0:
        return max(1, min(floor, expected))
    return floor


def content_timeout_ms(mode: str = "quote") -> int:
    return CONTENT_TIMEOUT_MS


def expected_cards(html: Optional[str]) -> Optional[int]:
    """How many rows this page says it holds, if it says.

    Google publishes no stated total — the payload carries the rows and
    nothing about a larger set they are drawn from — so this reports what
    was published rather than inventing a target.
    """
    if not html:
        return None
    return total_results(html, mode="markets")


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, threshold: int,
                   timeout_ms: int = CONTENT_TIMEOUT_MS,
                   interval_ms: int = 250) -> int:
    """Poll `count(selector)` until it reaches `threshold` or time runs out.

    Two things about this are deliberate and both were bugs in a sibling
    first:

      * It polls a COUNT through the driver's own protocol rather than
        waiting on an evaluated STRING. A site whose Content-Security-Policy
        lacks `unsafe-eval` kills `wait_for_function` with an `EvalError`
        and takes the run down with exit 1 — a crash, on the site's most
        obvious url. Google Finance ships a strict CSP with a nonce and no
        `unsafe-eval`, so this is not hypothetical here.
      * `found >= threshold` is the success case, NOT `>`. A page with
        exactly `threshold` matches is fully painted, and reporting it
        unpainted is only visible on a page with fewer rows than the floor —
        which is why it survived in this family until someone ran a thin
        query.
    """
    waited = 0
    found = 0
    while waited <= timeout_ms:
        try:
            found = count(selector)
        except Exception:  # a driver mid-navigation; try again
            found = 0
        if found >= threshold:
            return found
        sleep(interval_ms)
        waited += interval_ms
    return found


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "", mode: str = "quote") -> str:
    """Which of the states this response is.

    `status` is positional and comes SECOND, matching `detect_page_state`.
    Getting that wrong is not a style question: a sibling shipped two of
    three engines calling this as `classify(html, url=...)`, both crashed on
    their FIRST fetch, and nothing short of a live run or a
    signature-binding check saw it (§17). The suite here binds every engine's
    call against this signature for that reason.

    On this site the BODY is more load-bearing than the status, which is the
    reverse of a sibling: Google answers a refused client with HTTP 200 and a
    different page rather than with a status, so `unsupported_client` and
    `not_found` are both settled without one. Selenium, which cannot supply a
    status at all, is therefore not a degraded case here.
    """
    if html is None:
        return "blocked"
    return detect_page_state(html, status, url, mode)


# The retry/solve/blocked decision as DATA rather than as three copies of an
# if-chain in three engines (§1).
#
#   parse    is there anything on this page worth writing down?
#   retry    would fetching it again plausibly help?
#   solve    is there something to pay a solver for?
#   blocked  does this count towards exit 3?
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content": {"parse": True, "retry": False, "solve": False, "blocked": False},

    # A DEFINITE answer, and the four False values are the whole point.
    #
    # Not parsed: the page carries 35 instrument records of chrome and none
    # of them is the symbol that was asked for, so parsing it would write
    # plausible phantom rows for a symbol that does not exist.
    #
    # Not retried: the site has considered the question and answered it. A
    # second fetch buys the same answer, and a rotation buys it from a
    # different address. The caller's symbol list is what is wrong — which
    # is a thing worth telling them, so the engines name the symbol in the
    # `stop_reason` rather than reporting a generic empty page.
    #
    # Not blocked: nobody refused anything. Counting this as exit 3 would
    # send a reader to buy a proxy because they typed `EURUSD:CURRENCY`
    # instead of `EUR-USD` — which is exactly the typo this site invites,
    # since the wrong spelling returns HTTP 200 with the ticker echoed back
    # 17 times.
    "not_found": {"parse": False, "retry": False, "solve": False, "blocked": False},

    # The CLIENT was refused, not the address, so this is NOT `blocked` and
    # must never be reported as one: no proxy, no exit country and no solve
    # changes it, and every engine in this repo sets a browser User-Agent,
    # so reaching this state at all points at a misconfiguration rather than
    # at the site. Retried once, because the redirect is cheap and a single
    # transient miss is possible; `block_advice` says what to actually do.
    "unsupported_client": {"parse": False, "retry": True, "solve": False, "blocked": False},

    # Served exactly as asked, nothing in it. Distinct from `not_found`
    # because the site said nothing — this is our own reading of a page it
    # served. Not retried: a second identical fetch answers identically.
    "empty": {"parse": False, "retry": False, "solve": False, "blocked": False},

    # Retryable, NOT blocked, and not paid for. Wants a wait at the SAME
    # exit; reporting "blocked" for a page that would have come back on its
    # own sends a reader to buy a proxy they do not need.
    "throttled": {"parse": False, "retry": True, "solve": False, "blocked": False},

    "challenge": {"parse": False, "retry": True, "solve": True, "blocked": False},
    "blocked": {"parse": False, "retry": True, "solve": False, "blocked": True},

    # Served, built out of Google's own assets, and not recognised. Retried
    # once rather than parsed: an unrecognised page is the one state where
    # another fetch genuinely might differ.
    "unknown": {"parse": False, "retry": True, "solve": False, "blocked": False},
}


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["parse"]


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["blocked"]


def is_definite_answer(state: str) -> bool:
    """States where the SITE has answered and no budget should be spent.

    `not_found` is the whole reason this exists: it is neither content nor a
    failure, and an engine that lumps it in with `empty` reports exit 4 for
    three good symbols because a fourth was misspelt.
    """
    return state == "not_found"


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Whether this page is served but has not filled its grid in yet.

    Always False on this site, and that is measured rather than lazy: the
    payload is in the first response, so a page that was served and parsed
    to nothing is not going to improve by being waited on. Kept as a
    function because the engines call it and because a future Google change
    would land here rather than in three engines.
    """
    return False


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------
# What gets refused on this site is measured, and it is NOT the address.
# 2026-09-22, one Hetzner datacentre address in Helsinki, one fetch each:
#
#   plain HTTP, curl's own User-Agent      ->  /finance/beta/unsupported,
#                                              657 KB, no data
#   plain HTTP, a Chrome User-Agent        ->  the real page, 1.31 MB
#   headless Chromium                      ->  the real page
#
# `Sec-Fetch-Dest/Mode/Site` made no difference in either direction. So the
# thing that decides the answer here is whether the client claims to be a
# browser — the INVERSE of a sibling whose edge refuses a curl handshake
# precisely FOR claiming to be Chrome. Neither is a general rule; the axis is
# what to measure.
#
# That is why the block budget is small: no challenge was met in 11 fetches
# from a datacentre address, so if a consistent browser client IS being
# refused, another address is a guess and `block_advice` says what to check
# first.
#
# The engines read these constants rather than computing their own budget —
# a policy constant nothing consults is the same defect as dead code (§17),
# and the suite asserts each one has a consumer.
RETRY_ON_BLOCKED = True
BLOCK_RETRIES_WITHOUT_POOL = 1
BLOCK_RETRIES_WITH_POOL = 3

# A throttle's own budget, separate from the block budget because the right
# response is the opposite one: wait longer at the SAME exit rather than
# move. No 5xx was observed in testing; the budget exists because a run at
# volume will meet one and because counting it as a block is the expensive
# mistake.
THROTTLE_RETRIES = 3
THROTTLE_BACKOFF_MS = (6_000, 15_000, 30_000)

# One solve per page, and on this site the honest number so far is zero: no
# captcha of any kind was rendered across the 11 captures of 2026-09-22, and
# every candidate marker scored 0 on all of them.
#
# That is NOT a claim that Google does not challenge scrapers. It serves
# `/sorry/index` with a reCAPTCHA to addresses it does not like, this repo
# recognises that page, and 2Captcha solves that task type — saying anything
# narrower would be the mistake CLAUDE.md §19 calls the worst bug in this
# family, where a README told readers a key would not help on a site whose
# captcha the vendor had solved for years.
#
# The cap is kept, and kept ENFORCED through `solve_budget`, because a
# sibling found this constant had read like a limit in every repo in the
# family while every engine called the solver twice per attempt and counted
# once — one page bought three solves (§23).
SOLVES_PER_PAGE = 1


def solve_budget(spent: int) -> bool:
    """Whether another solve on this page is within the cap.

    Every call site that might solve goes through this, including the one
    BEFORE the page is classified. Counting only the post-classification
    call is exactly how a sibling paid three times for a one-solve cap.
    """
    return spent < SOLVES_PER_PAGE


def throttle_delay_ms(attempt: int) -> int:
    idx = max(0, min(attempt, len(THROTTLE_BACKOFF_MS) - 1))
    return THROTTLE_BACKOFF_MS[idx]


def block_advice(html: Optional[str], headless: bool,
                 has_pool: bool) -> str:
    """What a reader should actually DO about this state.

    Exists because the honest answer differs per site and a generic "try a
    proxy" wastes an afternoon where it is wrong. Here it is wrong twice
    over: the commonest refusal on this site is not about the address at
    all, and the second commonest is not a refusal.
    """
    if is_unsupported_client(html or ""):
        return (
            "unsupported client. Google Finance redirected this request to "
            "/finance/beta/unsupported, which it serves to a client whose "
            "User-Agent does not look like a browser. This is NOT a block: "
            "no address was refused and no proxy, exit country or captcha "
            "solve will change it. Measured 2026-09-22 from one datacentre "
            "address, one fetch each: curl with its own User-Agent got the "
            "unsupported page, curl with a Chrome User-Agent got the real "
            "1.31 MB page, and headless Chromium got the real page. Every "
            "engine in this repo sets a browser User-Agent, so seeing this "
            "means something overrode it — check --user-agent and any "
            "fingerprint options before changing anything else.")

    if is_not_found(html or ""):
        return (
            "not found. Google says it has no such instrument — this is an "
            "answer, not a failure, and retrying or rotating will get the "
            "same one. Check the symbol's spelling: venue-traded "
            "instruments are TICKER:EXCHANGE (GOOGL:NASDAQ, .INX:INDEXSP, "
            "GCW00:COMEX) while currency and crypto pairs are BASE-QUOTE "
            "(EUR-USD, BTC-USD). The colon form of a pair is the trap — "
            "EURUSD:CURRENCY returns HTTP 200 with this page and echoes the "
            "ticker back 17 times, so it reads like a served quote.")

    marker = detect_block_marker(html or "") or "no marker"
    return (
        "blocked (%s). This is Google's own interstitial, which it serves to "
        "addresses it scores badly rather than to clients it dislikes — the "
        "opposite axis from the unsupported-browser page above. No challenge "
        "was met across 11 fetches from a datacentre address on 2026-09-22, "
        "so meeting one means this address is being scored. %s Note that "
        "--mode markets and --mode movers need no credential of any kind on "
        "this site, so a block there is worth reporting as a site change "
        "rather than worked around quietly."
        % (marker,
           "Rotate with --proxy-rotate." if has_pool
           else "A residential --proxy is the next thing to try; a 2Captcha "
                "Scraping Browser endpoint brings its own exit and its own "
                "identity, so use one or the other and never both."))


# ---------------------------------------------------------------------------
# Pagination — there is none, and that is the contract
# ---------------------------------------------------------------------------
# CLAUDE.md §7 wants three layers, weakest last. On this site all three are
# absent, and the honest thing is to say so in code rather than to ship a
# selector that can never match:
#
#   1. `<link rel="next">` — ABSENT on all 11 captures.
#   2. A rebuildable `?page=N` — ABSENT. The seven `/finance/markets/*` urls
#      redirect to the root, and a quote url addresses one instrument.
#   3. A data terminator — not needed, because there is never a second page
#      to terminate.
#
# So `NEXT_PAGE_SELECTOR` is empty rather than hopeful. A sibling shipped
# three selectors that were already dead and reported a complete run holding
# a third of the data; an empty selector that the engines check against
# `pagination_is_addressable` cannot do that.

NEXT_PAGE_SELECTOR = ""


def next_page_selector(page_num: int = 1) -> str:
    return NEXT_PAGE_SELECTOR


def requested_page_number(url: str, loop_index: int = 1) -> int:
    """Which page the FETCHED url asks for. Always 1 here.

    A sibling's version of this compared the loop counter against the served
    page and threw away 45 good rows on a run that started at `?p=2`. There
    is no such parameter on this site, so the comparison is trivially true —
    and it is kept, rather than removed, so that an engine's page loop reads
    the same here as in its siblings.
    """
    return 1


def served_page_number(html: Optional[str] = None,
                       url: str = "") -> Optional[int]:
    return 1


def served_the_page_asked_for(html: Optional[str], asked_for: int = 1,
                              url: str = "") -> bool:
    """Did the site serve the page we asked for?

    Always True, because there is only ever one page. This is the check two
    siblings needed because their sites re-served page 1 for an
    out-of-range request under HTTP 200; it cannot fire here and is kept
    honest rather than deleted, so the engines' loop is identical.
    """
    return True


def pagination_is_addressable(url: str = "", html: Optional[str] = None) -> bool:
    return paginates_by_url(url, html)


def comparable(url: str) -> str:
    """A url reduced to what makes two urls the same listing."""
    return canonical_url(url)


def next_page_candidates(current_url: str,
                         next_href: Union[str, Iterable[str], None] = None,
                         page_num: int = 1) -> List[str]:
    """Empty, always. There is no next page on this site."""
    return []


def cap_summary(html: Optional[str], url: str = "") -> Dict[str, Optional[object]]:
    """What the sidecar records about the shape of this read.

    `capped_by_site` is True and `reachable_max` is 1 on every url here, and
    that is not the sibling's meaning of the words. There it meant "the site
    will serve you 15 of 1,268 pages and calling that complete is a lie by
    omission". Here it means the opposite: one page IS everything the site
    publishes for this url, so a one-page run is exhaustive rather than a
    sample. `market` is what a consumer actually needs to know, because the
    root page's lists are geo-selected and two markets are two samples.
    """
    summary: Dict[str, Optional[object]] = {
        "total_results": total_results(html, mode=listing_kind(url)) if html else None,
        "pages_available": 1,
        "capped_by_site": True,
        "reachable_max": 1,
        "paginates": False,
    }
    summary.update(market_metadata(html, url))
    return summary


def concurrency_limit(url: str = "") -> Optional[int]:
    """The highest `--concurrency` a single url can honestly support.

    1, always — there is one page per url, so handing pages to workers is
    meaningless.

    This is NOT the same as saying the repo is serial. `--mode quote` over a
    `--symbols` list is genuinely parallel work, one url per symbol, and the
    engines split THAT across workers. The distinction matters because a
    sibling refused `--concurrency` outright on an unpaginated route and
    lost the parallelism it could have had.
    """
    return 1


def concurrency_refusal(url: str = "") -> Optional[str]:
    """Why per-PAGE concurrency is refused for this url, or None.

    Refused with the reason (§18) rather than silently honoured as 1, which
    would look like the flag did something.
    """
    reason = unsupported_reason(url)
    if reason:
        return reason
    return ("%s — so --concurrency splits SYMBOLS, not pages. Pass more "
            "symbols to use it." % no_pagination_reason(url))
