"""google-finance-scraper — pyppeteer edition.

A parity engine. `playwright_scraper.py` is primary and is the only one with
`--concurrency`; this one exists so that the family's behaviour is not a
property of one driver. It must agree with its twins on exit codes, run
status, and whether a run crashes or spends money — `finish_run()` in
output_writer.py is what keeps that mapping from drifting.

pyppeteer is effectively unmaintained and its own README points at
Playwright. It adds one flag its twins do not have, `--chromium-path`,
because it downloads its own Chromium and has to be told where a system one
is; the suite asserts that difference in both directions, so removing it
fails just as adding an undocumented one would.

See `playwright_scraper.py`'s docstring for what this site does and why the
engines are shaped the way they are. Two things specific to this driver:

* it imports `launch`/`connect` at MODULE level, deliberately. An engine
  that imports its driver inside the launch path imports cleanly with the
  library absent, so the offline suite's skip group never fires and the CI
  job that exists to fail on unexpected skips cannot catch a broken import;

* `page.authenticate` is how proxy credentials reach it — never
  `--proxy-server`, which puts them in the browser's command line where
  anything that can run `ps` reads them.
"""

import argparse
import asyncio
import concurrent.futures
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

# At module level, deliberately, and not inside the launch path where it
# started out. The offline suite guards `import puppeteer_scraper` behind
# try/except ImportError and REPORTS the skip, and CI's engine-smoke job fails
# on any reported skip — that whole mechanism only works if importing this
# module actually requires the driver. With the import hidden inside
# _Session.open(), the module imported cleanly with no pyppeteer installed at
# all, the group never skipped, and CI could not have noticed a broken import.
# It also let CI install pyppeteer 0.0.25 (a stub, resolved from an unpinned
# `pip install pyppeteer`) without anything failing, because nothing ever
# imported it.
from pyppeteer import launch, connect

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from product_parser import (parse_products, currency_from_page,
                            PAGE_CAP as parser_page_cap,
                            detect_bot_challenge, page_url, listing_kind,
                            capped_by_site, reachable_max,
                            is_supported_host, total_results,
                            search_header, unsupported_reason,
                            served_by_google,
                            quote_url, markets_url, with_market,
                            canonical_url, symbol_from_url,
                            no_pagination_reason, MARKET_STRIPS, MOVER_LISTS,
                            market_from_url, QUOTE_PAGE_MODES,
                            MARKET_PAGE_MODES)
from output_writer import (dedupe_by_key, finish_run, EXIT_API_ERROR,
                           DEDUPE_KEY_BY_MODE,
                           SOURCE_DEFAULT)
import page_flow
from page_flow import MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# A price-coverage floor. One number, not a per-mode map, because on this
# site there is one answer: every row the parser emits carries a price, and
# an index level counts as one.
#
# Measured 2026-09-22: 46 of 46 market rows priced, and every quote row in
# every live run since. So the floor is high on purpose — a run that comes
# back 80% priced has not met an unusual page, it has a broken read, and
# the warning should say so rather than shrug.
PRICE_FLOOR = {"markets": 95, "movers": 95}

# A unit holding less than this share of the fullest unit in the same run
# is reported as thin. Loose on purpose here: the units are SYMBOLS rather
# than pages of one listing, so they legitimately differ in size — a quote
# is one row and a chart is a hundred — and comparing them against each
# other says nothing. It stays because the shared loop reads it.
THIN_PAGE_SHARE = 0.6

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own and its page methods' `timeout` option does
# not cover a browser that has stopped answering at all.
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged. That module holds
    the policy all three engines must share (how long to wait for
    challenge, when to scroll, when only a fresh session helps) and it is
    written against plain synchronous callables — which is the right shape for
    two of the three drivers. Bridging here keeps the policy in one place
    rather than growing an async copy of it that would drift.

    The second benefit is the one the family's rules actually require: every
    call gets an explicit, enforced timeout. `.result(timeout)` returns
    control even when the browser never answers, which is not something
    pyppeteer's own API offers.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each one as "Future exception was never retrieved:
        # NetworkError('Protocol error Target.sendMessageToTarget: Target
        # closed.')" — at ERROR level, AFTER a successful run has printed its
        # results. Five of those under a "Saved 48 products" line read as a
        # failed run. Only that shape is swallowed; anything else still gets
        # the default handler, because silencing the loop wholesale would hide
        # real faults.
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # BOTH, not one or the other. asyncio puts its own words in
        # `message` ("Future exception was never retrieved") and the library's
        # in `exception` (a NetworkError about a closed CDP session), and an
        # `or` between them looks at the exception and never sees the message
        # — which is why these kept printing after they were "handled".
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                # asyncio's own words when the loop stops with work in
                # flight. Emitted after a successful run; see close().
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                # A CDP message addressed to a session that has gone away.
                # Routine over a remote browser: three of six captures of
                # this site had their target closed mid-scroll and succeeded
                # on the next attempt.
                "No session with given id",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING whatever it still has in flight.

        Stopping the loop outright leaves pyppeteer's own background tasks
        pending — its websocket reader and keepalive — and asyncio then prints
        "Task was destroyed but it is pending!" plus a traceback for each of
        them. That happens AFTER the output has been written, so the run is
        fine and the log looks like a crash. Four tracebacks under a
        successful run is how a reader learns to ignore the log.

        Cancelling first is the fix, and it has to happen ON the loop thread —
        `call_soon_threadsafe` is what gets it there.
        """
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            if pending:
                logger.debug("Cancelled %d pending pyppeteer task(s) on "
                             "teardown.", len(pending))
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # Populated here: the SSR payload states `total` and `lotsPerPage` on
    # page 1, which is what makes the page count arithmetic.
    total_available: Optional[int] = None
    # The raw result-count header, verbatim, for the sidecar.
    header: Optional[str] = None
    # The shape of this read, for the sidecar: total_results,
    # pages_available, reachable_max, capped_by_site, paginates, market. On
    # this site `capped_by_site` means the OPPOSITE of what it means in the
    # family's paginated repos — one page IS everything the url publishes,
    # so a one-page run is exhaustive rather than a sample — and `paginates:
    # false` beside it is what says which.
    cap: Optional[dict] = None
    # Whether the SERVER served the page we asked for, read off
    # `pagination.start` rather than trusted from the request. False means
    # the end of a listing on the sites in this family that have one. This
    # site has none — every url addresses exactly one thing — so it is
    # always True here and the field stays for the shared outcome shape.
    served_requested_page: bool = True
    # In --mode product, the seller's own id/name/slug from the page's Apollo
    # cache. Stored as the small dict rather than by keeping the page's HTML
    # around: a detail page is 285 KB and a scrolled listing nearly 1 MB.
    shop_facts: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a driver's connection
# error can repeat the endpoint several times (the message plus a call log),
# so a masker that handled only the first occurrence would print the password
# the other times and look like it was working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Takes arbitrary text, not just a URL, because the strings that most need
    this are exception messages with a URL inside them. The host and port are
    KEPT — which endpoint or exit a run used is the useful half of the line
    and is not the secret.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


# `_chrome_ua` is deliberately ABSENT from this engine, where the other two
# have it. Not an oversight and not a parity gap to close: setting a UA
# through pyppeteer's CDP override is measured to get this engine refused
# from its second navigation onward, because the override does not carry the
# matching User-Agent Client Hints. See `_Session.open` for the four-variant
# measurement. The suite asserts this engine sets no user agent, so
# reintroducing one becomes a decision rather than a regression.


class _Session:
    """One pyppeteer browser + page, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser: cookies a bot
    manager issued against one exit, replayed from another, are a stronger
    signal than either address alone, so the cookie jar goes with the exit.
    """

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            # pyppeteer's browserWSEndpoint takes the full ws://user:pass@host
            # form and authenticates on the WebSocket upgrade, so an
            # authenticated Scraping Browser endpoint works here — unlike
            # Selenium's debuggerAddress, which has nowhere to put a password.
            try:
                self.browser = self.bridge.run(
                    connect(browserWSEndpoint=self.args.cdp_endpoint,
                            ignoreHTTPSErrors=True), timeout=CONNECT_TIMEOUT)
                self.page = self.bridge.run(self.browser.newPage())
            except Exception as e:
                # REPORTED, not raised through. A bounded remote call that
                # gives up is an expected outcome of this engine, and letting
                # it unwind printed a 40-line asyncio traceback where the
                # Playwright engine prints one sentence and exits 5 —
                # "reporting a timeout is not the same as exiting on one"
                # (§8), and the three engines must agree on exit codes.
                #
                # The message is masked on the way out: pyppeteer and
                # websockets both put the endpoint they tried into the
                # exception text, and that endpoint is a URL with the
                # password in it.
                raise RuntimeError(
                    "could not connect to --cdp-endpoint %s: %s\n"
                    "A Scraping Browser profile allows ONE live connection "
                    "at a time, so a timeout here usually means another run "
                    "still holds this `pid`. Wait for it to finish, or use a "
                    "different pid."
                    % (_mask_credentials(self.args.cdp_endpoint),
                       _mask_credentials("%s: %s" % (type(e).__name__, e)))
                ) from None
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the Chromium at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= becomes part of the browser's
            # argv, readable by anything that can run `ps`.
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))

        # handleSIGINT/TERM/HUP off, and not for tidiness: pyppeteer installs
        # signal handlers inside launch(), and `signal.signal` raises
        # "signal only works in main thread of the main interpreter" because
        # the event loop here lives on a worker thread. Teardown is handled by
        # _Session.close() in scrape()'s finally block instead, so nothing is
        # lost — the browser is still closed on both success and failure.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        # NO setUserAgent here, and this is the most expensive measurement
        # in this repo. It is a deliberate DIVERGENCE from the Playwright
        # engine, which does set one, and it is required for this engine to
        # work on this site at all.
        #
        # WHAT was measured, and then separately what it probably means,
        # because the second part is an inference and the first is not.
        #
        # Measured 2026-09-21, four consecutive navigations per variant,
        # same browser, same address:
        #
        #   no override at all           4 of 4 served, payload present
        #   Windows UA override          nav 1 served, navs 2-4 DENIED
        #   Linux UA override (matching
        #   the real platform)           nav 1 served, navs 2-4 DENIED
        #
        # Note the third row, because it is what settles the cause: a UA
        # that agreed with the platform was refused just as hard. It is not
        # Windows-on-Linux, it is the override. Note also that nav 1 always
        # passes, which is why a one-page run of this engine looked fine and
        # a two-page run did not — and why this needed a live run rather
        # than a reading (§16).
        #
        # The cost of not overriding is that the UA says "HeadlessChrome".
        # On most sites that is a giveaway; on this one it is measured to be
        # served 4 times out of 4, while the disguise is measured to be
        # refused 3 times out of 4. Take the measurement over the instinct.
        #
        # The likely mechanism, stated as the inference it is: pyppeteer
        # overrides through `Network.setUserAgentOverride` with no
        # `userAgentMetadata`, so `navigator.userAgent` changes while the
        # User-Agent Client Hints (`Sec-CH-UA`, `navigator.userAgentData`)
        # keep reporting the real browser, and the page claims two clients
        # at once.
        #
        # But that explanation is NOT sufficient on its own, and the
        # counter-example is in this repo: `selenium_scraper.py` issues the
        # same `Network.setUserAgentOverride`, with the same absent
        # metadata, and was served 3 of 3 pages on a live run. So something
        # else about how pyppeteer drives the page is involved, and this
        # comment stops at the boundary of what was measured rather than
        # inventing the rest. If you find the real cause, the fix may be to
        # set the metadata and put the override back — with a live
        # multi-page run as the evidence.
        self.bridge.run(self.page.setViewport({"width": 1600, "height": 1000}))
        if self.args.locale:
            # pyppeteer has no context-level locale, so it goes in the
            # headers. Same effect for this site, which reads the language
            # off `?lang=` and ignores Accept-Language for pricing.
            self.bridge.run(self.page.setExtraHTTPHeaders(
                {"Accept-Language": self.args.locale}))
        if self.args.fingerprint:
            self._apply_fingerprint()
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        return self

    def _apply_fingerprint(self):
        """Apply a 2captcha fingerprint to this page.

        The same patch script the Playwright engine installs on its context,
        applied through pyppeteer's own two primitives. Shared deliberately:
        two engines applying different halves of one fingerprint would be a
        contradiction of exactly the kind a fingerprint exists to avoid.

        `userAgent.value` is the key the API actually returns — reading a
        different one is how `--fingerprint` shipped in this family for
        months setting no user agent at all while reporting success (§16).
        """
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key,
                             tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        try:
            if ua:
                self.bridge.run(self.page.setUserAgent(ua))
            self.bridge.run(self.page.evaluateOnNewDocument(
                playwright_init_script(fp)))
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except Exception as e:                 # pyppeteer raises broadly
            logger.warning("Could not apply the fingerprint (%s) — "
                           "continuing with the browser's own identity.", e)

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to pyppeteer
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here; every decision about what to do
# with the answer is in page_flow.py so all three engines make it the same way.
def _driver(session):
    bridge, page = session.bridge, session.page

    def count(selector):
        return len(bridge.run(page.querySelectorAll(selector)))

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return bridge.run(page.content())
        except Exception as e:  # noqa: BLE001
            # A geo-redirect or the consent layer can navigate, so a
            # snapshot can land exactly on the document swap. None tells the
            # caller to skip a check rather than fail the run.
            logger.debug("content() unavailable (page navigating?): %s", e)
            return None

    def current_url():
        return page.url

    # No scroll primitives and no `page_height`, and that is measured on
    # THIS site rather than inherited: the same listing URL fetched by plain
    # HTTP with no JavaScript at all and by a real browser parsed to the
    # same 45 rows, with identical skus and prices. Every column comes out
    # of `window.__INITIAL_STATE__`, which is in the first response, so a
    # scroll here would be latency bought for nothing.
    return {"count": count, "sleep": sleep, "content": content,
            "current_url": current_url}


def _content(session) -> Optional[str]:
    return _driver(session)["content"]()


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    """Rows for this page, always as a list.

    The three modes read three different structures out of two page kinds,
    and routing them through one helper is what keeps everything downstream
    — dedupe, merge, coverage logging, the writers — working on one shape.

    Crossing them is a SILENT failure rather than a loud one, and this
    function proved it during the build: two engines kept a stale router
    that called the parser with no `mode` at all, so `--mode markets`
    defaulted to the quote path, found no rich record matching a root url,
    and reported 0 rows with exit 4 on a page that parses to 46. Nothing
    raised. `--help` was fine, the module imported, the offline checks were
    green, and only running all three engines against the same url found
    it — CLAUDE.md §16: mirroring is a design rule, not a verification.

    `page_num` is threaded through rather than defaulted. On this site it
    indexes the SYMBOL rather than a page, and without it every row of a
    three-symbol run would claim page 1 and position 1.
    """
    market = args.market or market_from_url(url)
    rows = parse_products(html, url, page=page_num, mode=args.mode,
                          market=market)
    # `listing` is a column on the row class the market LIST modes share,
    # and `earnings` is a market mode whose row class has no such column —
    # so `--mode earnings --category gainers` was an AttributeError on a
    # live run. Guarded here and refused in parse_args, because a filter
    # that cannot apply should be told to the caller rather than crashed on.
    if args.category and rows and hasattr(rows[0], "listing"):
        rows = [r for r in rows if r.listing == args.category]
    return rows


def _same_url(a: str, b: str) -> bool:
    """Whether two URLs address the same page.

    Delegates to page_flow rather than reimplementing the comparison, so all
    three engines cannot drift on it. On this site the comparison has to
    strip a long tracking tail: a listing anchor arrives with
    `?extParam=…keyword=kopi&search_id=…&src=search` and a detail page's own
    canonical arrives with a UTM triple, so two views of one page never
    match unless both sides are cleaned. An engine with its own copy of
    this in a sibling repo got the equivalent wrong and silently fell back
    to sequential fetching.
    """
    return page_flow.comparable(a) == page_flow.comparable(b)


def _next_page_candidates(session, page_num: int) -> List[str]:
    """The site's own next-page link, resolved, or None.

    Returns EVERY candidate, filtered by page_flow to the ones that really do
    paginate a listing, on the family's sites that have one: a chip rail
    alongside its items, and following that one returns rows from the wrong
    listing while reporting success.

    Reads the DOM's `.href` property rather than the raw attribute, which the
    browser has already resolved — the opposite of Playwright's
    get_attribute("href"). Kept explicit because the two engines differ here
    and a hand-rolled join got it wrong once.
    """
    # An EMPTY selector is not a selector, and handing one to
    # querySelectorAll is a DOMException rather than an empty result.
    # `NEXT_PAGE_SELECTOR` is deliberately "" on this site because there is
    # no next page to advertise, so this scan has nothing to do — and the
    # crash only surfaced once the symbol-queue fix let a run continue past
    # a row-less symbol and reach this line at all.
    if not page_flow.next_page_selector(page_num):
        return []

    bridge, page = session.bridge, session.page
    hrefs = bridge.run(page.evaluate(
        "(selector) => Array.from(document.querySelectorAll(selector))"
        ".map(a => a.href || a.getAttribute('href')).filter(Boolean)",
        page_flow.next_page_selector(page_num)))
    return page_flow.next_page_candidates(page.url, hrefs or [])


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same two families, same order, same "detected is not blocking" rule as
    the Playwright engine — see its docstring for why the anchor count is
    checked here rather than after the readiness wait.
    """
    bridge, page = session.bridge, session.page
    html = _content(session)
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = len(bridge.run(page.querySelectorAll(selector)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: bridge.run(page.evaluate(js)), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False
    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it.", challenge.kind, challenge.source,
                    already_rendered)
        return False
    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be solved.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001
        logger.error("Solving the challenge failed (%s).", e)
        return False
    bridge.run(page.evaluate(INJECT_TOKEN_JS, token))
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    bridge.run(page.reload({"waitUntil": "domcontentloaded", "timeout": 60000}))
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    The retry/rotate/wait policy is page_flow's and finish_run's; what differs
    here is only the driver calls. Kept structurally parallel on purpose —
    the two files are meant to be diffable, because "all three engines agree"
    is checked by reading them side by side as well as by the smoke suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    bridge, page = session.bridge, session.page
    d = _driver(session)

    # See the Playwright engine for the measurement: without a pool there is
    # no exit to rotate to, but a plain re-fetch is what clears a block on a
    # Scraping Browser profile, so the budget is not zero.
    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not just documented. It was a
    # constant with a paragraph of justification that no engine read — a
    # policy statement nothing enforced, which is the same defect as dead
    # code that looks load-bearing. Setting it False now really does stop
    # the retry loop.
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0
    # A THROTTLE's own budget, kept apart from the block budget
    # because the right response is the opposite one — see the
    # `throttled` branch below.
    throttle_spent = 0
    html, state, load_failed = None, "ok", False

    # The loop may be re-entered for a throttle without consuming a
    # block attempt, so the budget is a while rather than a for.
    block_attempt = -1
    while block_attempt < block_retries:
        block_attempt += 1
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        load_failed = False
        # The RESPONSE, not just the navigation — see the note in
        # playwright_scraper.py. Without the status a regional 403 reads as
        # `unknown` and ends the run as exit 4, which means "the page loaded
        # and there was nothing in it".
        status = None
        for attempt in range(1, args.retries + 1):
            try:
                response = bridge.run(
                    page.goto(url, {"waitUntil": "domcontentloaded",
                                    "timeout": 60000}))
                status = getattr(response, "status", None) if response else None
                load_failed = False
                break
            except Exception as e:  # noqa: BLE001 — pyppeteer raises many types
                load_failed = True
                # pyppeteer surfaces a dead proxy as a page error whose text
                # carries Chromium's own name for it, exactly as Playwright
                # does; a timeout and an unusable exit want opposite
                # responses, so they are told apart by that text.
                text = str(e)
                if any(marker in text for marker in _PROXY_ERROR_MARKERS):
                    logger.warning("Exit %s is unusable (%s).",
                                   mask(pool.current) if pool else "(none)", text[:120])
                    break
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        if load_failed and block_attempt < block_retries:
            pool.advance("unusable exit or repeated load failure")
            session.relaunch()
            bridge, page = session.bridge, session.page
            d = _driver(session)
            continue
        if load_failed:
            break


        # Counted against the SAME budget as the post-classification call
        # below, through page_flow.solve_budget. The ancestor of this engine
        # called the solver here WITHOUT counting and counted only at the
        # second call site, so `SOLVES_PER_PAGE = 1` read like an enforced
        # cap in every repo of this family while one page could buy two
        # solves — measured at three on a site where a challenge rendered on
        # every fetch (§23). Invisible wherever a challenge is rare, which
        # is everywhere until it is not.
        if page_flow.solve_budget(solves_bought):
            if handle_captcha_if_present(session, args):
                solves_bought += 1
                time.sleep(1)

        html = _content(session) or ""
        state = page_flow.classify(html, status=status, url=page.url)

        # "Not painted yet" is not a fault, and on this site it is also
        # not the usual case: a listing page carries all 45 of its products
        # in the first response, so `shell` means the payload was genuinely
        # absent rather than late. Waiting is still the right answer for it
        # — refetching a shell just buys another shell — and re-classifying
        # BEFORE the retry decision is what stops a slow page spending a
        # block retry. See page_flow.is_unpainted.
        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is a shell the site served but has not "
                        "filled in (%d bytes, empty cards) — waiting up to "
                        "%.0fs for the prices rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            need = page_flow.min_matches(args.mode, page_flow.expected_cards(html))
            found = page_flow.wait_for_count(
                d["count"], d["sleep"], page_flow.ready_selector(args.mode),
                need, wait_timeout)
            if found < need:
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_timeout / 1000, found)
            html = _content(session) or html
            state = page_flow.classify(html, status=status, url=page.url)

        # No interstitial-settling step, and its absence is measured rather
        # than an omission: this site has no interstitial. A refused request
        # gets a 394-byte "Access Denied" with nothing on it to settle, and
        # what triggers it is the browser being headless, not the address. See page_flow's "There is no block page".
        #
        # The paid path is reached only for state "challenge", which no
        # capture of this site has ever produced. Wired up because a bot
        # manager can be switched on between deploys, and bounded by
        # SOLVES_PER_PAGE so a speculative path cannot become a bill.
        if (page_flow.should_solve(state)
                and page_flow.solve_budget(solves_bought)):
            solves_bought += 1
            if handle_captcha_if_present(session, args):
                time.sleep(1)
                html = _content(session) or html
                state = page_flow.classify(html, status=status, url=page.url)
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — a hub category has no grid — so retrying it
            # would re-confirm the same right answer, and rotating the exit
            # would blame an address for the URL it was given.
            break

        if state == "throttled":
            # Wait longer at the SAME exit, on the throttle's own budget.
            # Google answers a client going too fast with HTTP 503 and its
            # own branded page — and serves the IDENTICAL body under 403 on a
            # route it refuses outright, so the status is the only thing
            # separating them. Deliberately does not consume a block attempt
            # and does not rotate: moving exit would abandon an address that
            # was working, and counting it as blocked would report exit 3 for
            # a page that was about to come back. Measured: a URL that had
            # just 503'd twice came back 200 with a full payload on 4 of 4
            # tries once 6 seconds were put between requests.
            if throttle_spent < page_flow.THROTTLE_RETRIES:
                throttle_spent += 1
                pause = page_flow.throttle_delay_ms(throttle_spent) / 1000
                logger.warning(
                    "Page %d came back throttled (HTTP 5xx, Google's own "
                    "\"access is concentrated\" page). That is a rate limit "
                    "rather than a refusal, so waiting %.0fs and re-fetching "
                    "from the same exit (%d/%d). Raise --delay if it keeps "
                    "happening.", page_num, pause, throttle_spent,
                    page_flow.THROTTLE_RETRIES)
                time.sleep(pause)
                block_attempt -= 1
                continue
            logger.error(
                "Page %d is still throttled after %d wait(s). Google Finance is "
                "rate-limiting this client rather than refusing it, so the "
                "fix is fewer requests (raise --delay, lower --concurrency) "
                "rather than another exit.", page_num, throttle_spent)
            break

        # Blocked or challenged. The ADDRESS is what was scored, not the URL,
        # so a different exit is the only thing that plausibly changes the
        # outcome.
        if block_attempt < block_retries:
            if pool is not None:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit (%d/%d).", page_num, state,
                               mask(pool.current), block_attempt + 1,
                               block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
                bridge, page = session.bridge, session.page
                d = _driver(session)
            else:
                # No pool, so nowhere else to go — but a plain re-fetch does
                # clear this sometimes, and this branch is REACHABLE here:
                # `page_flow.BLOCK_RETRIES_WITHOUT_POOL` is 1 on this site,
                # against 0 in the sibling repo this engine was ported from,
                # and `mask(pool.current)` on a None pool took a live run
                # down with an AttributeError the moment it was. Mirrors
                # playwright_scraper exactly.
                pause = args.retry_delay * (block_attempt + 1)
                logger.warning("Page %d came back as %s — re-fetching through "
                               "the same access path in %.1fs (%d/%d).",
                               page_num, state, pause, block_attempt + 1,
                               block_retries)
                time.sleep(pause)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        # There is no challenge on this site to solve. Akamai answers a
        # refused request with HTTP 403 and a 394-byte "Access Denied" that
        # carries a reference id and nothing else, so a 2Captcha key does not
        # help and a REAL WINDOW does — not a better address. The dump is written even when empty: "0 bytes" is itself
        # the diagnosis here, and a reader who finds no file cannot tell that
        # from a run that never got this far.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset host, saved to %s. Akamai refuses this site with a "
            "43-byte body whose whole content is `Reference  #18.\u2026`, and "
            "it arrives under HTTP **200** — so the status code is not the "
            "signal and there is no widget on the page for any solver. What "
            "clears it is usually NOT a different address, measured "
            "2026-09-21: one datacentre IP was served the full 45-product "
            "page both by plain curl and by headless Chromium, and refused "
            "only when a curl handshake claimed a Chrome User-Agent. What "
            "Akamai refuses here is the CONTRADICTION between a claimed "
            "client and the TLS fingerprint underneath it, so check for a "
            "fingerprint you added before buying an exit. This is exit 3, "
            "distinct from a genuinely empty result (exit 4).",
            len(html or ""),
            "which references" if served_by_google(html or "")
            else "with no reference to", debug_html)
        outcome.blocked_by = "no-response" if not html else "not-served"
        outcome.final_url = d["current_url"]()
        return outcome


    # Through the POLICY, not against a literal. `STATE_POLICY` is the
    # one place that says which states are worth reading, and an engine
    # comparing against "content" by hand is an engine that can quietly
    # disagree with its twins and with the table — which is the whole
    # reason page_flow exists (CLAUDE.md §1). Found by checking which
    # public functions had no consumer: `should_parse` had none, while
    # its three siblings were all wired.
    if state == "not_found":
        # Named rather than left as a generic empty page. `STATE_POLICY`
        # says the caller's symbol list is what is wrong and that this is
        # worth telling them — a promise the engines were not keeping, so a
        # run of ten symbols with three typos reported seven rows and no
        # hint which three. The colon form of a pair is the trap this exists
        # for, because it returns HTTP 200 with the ticker echoed back.
        logger.warning(
            "Google has no instrument called %s — it served its own Page "
            "Not Found. This is an ANSWER, not a block: retrying or "
            "rotating an exit gets the same one. Venue-traded instruments "
            "are TICKER:EXCHANGE (GOOGL:NASDAQ); currency and crypto pairs "
            "are BASE-QUOTE (EUR-USD, BTC-USD).",
            symbol_from_url(url) or url)

    if page_flow.should_parse(state):
        # Wait for the prices to fill in. There is NO scroll: three scrolls
        # to the document's own bottom added zero cards on every page kind.
        # What the wait is for is hydration — the server sends 24 card shells
        # with 24 EMPTY price nodes, so a card count is satisfied instantly
        # while every price is still blank:
        # 95 after one scroll on the measured search URL.
        selector = page_flow.ready_selector(args.mode)
        threshold = page_flow.min_matches(args.mode,
                                          page_flow.expected_cards(html))
        # A POLL, not waitForFunction: a string handed to the browser is
        # refused outright by a CSP without `unsafe-eval`, and this one only
        # happens to allow it. See page_flow.wait_for_count.
        found = page_flow.wait_for_count(
            d["count"], d["sleep"], selector, threshold,
            page_flow.content_timeout_ms(args.mode))
        time.sleep(0.5)
        if found < threshold:
            # Not an error on its own, and what it MEANS depends on the
            # mode — which is why the message does too. A listing page with
            # no grid is a correct answer (a taxonomy hub, or one page past
            # the end); a detail page whose buy box never painted is a
            # different thing entirely, and on this site it is usually just
            # slow rather than absent, because the row is parsed out of the
            # page's JSON-LD and not out of the buy box.
            logger.info("No instrument links appeared within the readiness "
                        "timeout. On this site that is unusual rather than "
                        "expected: the rows come out of the page's own "
                        "AF_initDataCallback payload, which is in the FIRST "
                        "response, so the run can still return them. If it "
                        "returns none, read the state in the sidecar — "
                        "not_found means Google has no such symbol, and "
                        "unsupported_client means something overrode the "
                        "browser User-Agent.")

        html = d["content"]() or html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only when the page is NOT already content. A challenge marker on a page
    # whose ads have rendered guards nothing — and over --cdp-endpoint the
    # Scraping Browser's own auto-solve extension injects such markers into
    # every page it loads, which is why the scan strips extension <script>
    # tags first. Only for a state page_flow already counts as BLOCKED; an
    # EMPTY page is a correct answer. Mirrors playwright_scraper exactly.
    vendor = (detect_bot_challenge(html, url=page.url)
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        return outcome

    # Did the SERVER serve the page we asked for? Read off
    # `pagination.start`, never trusted from the request, because neither
    # out-of-range route on this site fails in a way the client can see:
    # `?p=151` of a 150-page query answers 301 to page 1 and then HTTP 200
    # with 45 real products, and `/category/{id}/?p=2` answers 200 with page
    # 1 without even redirecting (§23).
    if page_flow.pagination_is_addressable(url):
        # Dead here — nothing paginates. It read
        # `args.mode == "listing"`, and "listing" has never been
        # a mode in this repo, so this branch and three like it
        # had not executed once. One of the three was the
        # price-coverage floor.
        # Compared against the page the fetched URL actually ASKS for, not
        # against the loop's counter — a run started on a URL that already
        # carries `?p=2` asks the site for page 2 while calling it page 1 of
        # the run. See page_flow.requested_page_number.
        asked = page_flow.requested_page_number(url, page_num)
        outcome.served_requested_page = page_flow.served_the_page_asked_for(
            html, asked)
        if not outcome.served_requested_page:
            logger.info(
                "Asked Google for page %d and it served page %s — its own "
                "`pagination.start` says so. That is the end of this "
                "listing; the site re-serves page 1 with HTTP 200 rather than "
                "erroring. Dropping this page's rows, which are duplicates.",
                asked, page_flow.served_page_number(html))
            outcome.state = "empty"
            return outcome

    products = _parse_for_mode(html, page.url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if args.mode in MARKET_PAGE_MODES and page_num == 1:
        outcome.total_available = total_results(html)
        outcome.header = search_header(html)
        outcome.cap = page_flow.cap_summary(html)
        # Read once from page 1 and carried forward: Google Finance publishes the
        # JSON-LD block that names the currency on page 1 only, so without
        # this the first 45 rows carried "JPY" and the rest carried null for
        # products whose currency the site had already stated. Never
        # defaulted — a run that never got page 1 leaves it null.
        stated = currency_from_page(html)
        if stated:
            args._currency = stated
            logger.info("Page 1's structured data states the currency as %s "
                        "— carrying it to the rest of the run.", stated)
        if capped_by_site(html):
            logger.info(
                "The site says this query matches %s products and will serve "
                "%s of them. A run that reaches the last page is COMPLETE as "
                "far as the site is concerned and is a sample of what it "
                "matched — the sidecar records both numbers.",
                f"{outcome.total_available:,}", f"{reachable_max(html):,}")

    # Only where the mode's row class HAS a price. `earnings` is a market
    # mode and an EarningsEvent has no `price` column, so reviving this
    # branch — it had been dead behind a mode named "listing" — turned it
    # into an AttributeError on the first live earnings run. A market mode
    # is not automatically a priced one.
    if (products and args.mode in MARKET_PAGE_MODES
            and hasattr(products[0], "price")):
        priced = sum(1 for p in products if p.price is not None)
        share = 100.0 * priced / len(products)
        floor = PRICE_FLOOR.get(args.mode, 0)
        logger.info("Price coverage on page %d: %d/%d (%.0f%%); the measured "
                    "floor is %d%%.",
                    page_num, priced, len(products), share, floor)
        if share < floor:
            logger.warning(
                "Only %.0f%% of page %d carries a price, against a measured "
                "floor of %d%%. Every one of 405 rows across 11 captured "
                "pages had one, so this is the read breaking rather than the "
                "page being unusual — re-run with --dump-html.",
                share, page_num, floor)
        # NO rating coverage. `rating` is not a column on any row class here
        # — it was the donor site's — and it sat inside a branch keyed on a
        # mode named "listing" that this repo has never had, so it had never
        # executed. Un-deadening that branch to reach the price-coverage
        # floor beside it turned this into an AttributeError on the first
        # live run, which is how it was found.

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = page.url
    return outcome


# Chromium's own names for "the proxy is the problem, not the site".
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # `sku` alone in --mode quote, where one row IS one instrument.
    # The list modes need (sku, listing) as well: a stock can be a gainer
    # and among the most active in the same session, so one symbol lands in
    # two of the root page's lists — 2 symbols on the 2026-09-22 US capture
    # — and deduping on `sku` would drop whichever list was parsed second.
    dedupe_key = DEDUPE_KEY_BY_MODE.get(args.mode, "sku")
    # Every mode here is single-unit. On the family's paginated sites this
    # a category listing — ?page=N, the same tiles — and treating it as
    # single-page made `--mode shop --pages 2` fetch one page and report
    # "complete", which is the silent-success failure this family exists to
    # avoid. Found on the first live shop run.
    stop_reason = "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    if args.concurrency > 1:
        logger.warning("--concurrency is ignored in this engine: parallel page "
                       "fetching is implemented in playwright_scraper.py, "
                       "which is the primary engine. Running one page at a "
                       "time.")

    bridge = _AsyncBridge()
    session = None
    try:
        session = _Session(bridge, args, pool).open()
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None
        elif args.mode in QUOTE_PAGE_MODES:
            seen_keys.update(p.sku for p in first.products if p.sku is not None)

            # Same check as the Playwright engine: the ?page=N convention is
            # only used when the site's own link agrees with it, so a cursor
            # or token in pagination cannot be silently papered over.
            planned = list(getattr(args, "_symbol_urls", []) or [])[1:] or None

            # No link-following fallback on this site: there is no next page
            # to advertise, so a candidate scan would search every page for a
            # link that is never there. The urls come from --symbols and are
            # known before the first fetch, which is a stronger property than
            # the rebuildable ?page=N a sibling relies on — page 5's address
            # does not depend on page 4's content, or on any fetch at all.
            url = planned[0] if planned else ""
            for page_num in range(2, args.pages + 1):
                if pool and pool.rotates_per_page():
                    pool.advance(f"per-page rotation, page {page_num}")
                    session.relaunch()

                outcome = _fetch_one_page(session, args, pool, page_num, url)
                outcomes.append(outcome)
                if not outcome.ok:
                    stop_reason = ("page_load_timeout" if outcome.load_failed
                                   else f"blocked_{outcome.blocked_by}")
                    blocked = outcome.blocked_by is not None
                    break

                fresh_count = sum(1 for p in outcome.products
                                  if p.sku is None or p.sku not in seen_keys)
                seen_keys.update(p.sku for p in outcome.products
                                 if p.sku is not None)
                # The site said it served a different page than the one
                # asked for. A distinct stop_reason from `no_new_products`:
                # this one is "the listing ended and the site answered with
                # page 1 anyway". Checked FIRST, since a re-served page 1
                # would trip the dedupe-based terminator too and report the
                # vaguer reason.
                if not outcome.served_requested_page:
                    stop_reason = "end_of_listing"
                    break
                if not fresh_count and page_flow.empty_unit_ends_the_run(url):
                    logger.info("Page %d added no rows not already seen — "
                                "treating that as the end of the listing.",
                                page_num)
                    stop_reason = "no_new_products"
                    break
                if not fresh_count:
                    # A row-less SYMBOL ends nothing: the remaining symbols
                    # are independent of it, and treating this as the end of
                    # a listing stopped the queue on the first typo. See
                    # page_flow.empty_unit_ends_the_run for the reproduction.
                    logger.info("%s returned no rows; the remaining symbols "
                                "are unaffected.", url)

                if page_num < args.pages:
                    nxt = _next_page_candidates(session, page_num)
                    url = (planned[page_num - 1] if planned else
                           (nxt[0] if nxt
                            else page_url(session.page.url, page_num + 1)))
                    time.sleep(args.delay)
    finally:
        if session is not None:
            session.close()
        bridge.close()

    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    # Completeness, checked over the MERGED result rather than per page — a
    # per-page check cannot see a gap BETWEEN two pages, which is exactly
    # where a short page hides.
    #
    # NOT "pages x rows-per-page", which is what the sibling repo does and
    # what fires on healthy runs here. This site's page size is steady:
    # a three-page run returned 62, 60 and 62 rows, and multiplying the
    # largest page by the page count then declared the run short by 8. A
    # threshold that warns on every healthy run teaches the reader to ignore
    # it.
    #
    # What is worth warning about is a page that came back materially THIN
    # against its siblings — that is what a truncated response or a
    # half-painted grid looks like. A page holding less than 60% of the
    # fullest page is well outside the +-3% spread that the varying page size
    # accounts for.
    total_available = next((o.total_available for o in outcomes
                            if o.total_available is not None), None)
    if args.mode in MARKET_PAGE_MODES and all_rows:
        counts = [(o.page_num, len(o.products)) for o in outcomes if o.ok]
        fullest = max((n for _, n in counts), default=0)
        thin = [(p, n) for p, n in counts
                if fullest and n < THIN_PAGE_SHARE * fullest]
        # The LAST page of a listing is legitimately short — the catalogue
        # simply ran out — so it is excluded unless there are pages after it.
        last_page = max((p for p, _ in counts), default=0)
        thin = [(p, n) for p, n in thin if p != last_page]
        if thin:
            logger.warning(
                "Page(s) %s came back much thinner than the fullest page "
                "(%d rows): %s. A truncated response or a half-painted grid "
                "looks like this — re-run with --dump-html to check the "
                "snapshot for those pages.",
                ", ".join(str(p) for p, _ in thin), fullest,
                ", ".join("page %d: %d" % (p, n) for p, n in thin))
        if total_available:
            logger.info("This listing holds %d product(s) in total; this run "
                        "took %d (%.1f%%).", total_available, len(all_rows),
                        100.0 * len(all_rows) / total_available)

            # The pages the CATALOGUE has that the site will NOT address.
            # A run that stops at the cap is complete as far as the site is
            # concerned and truncated as far as the catalogue is, and only
            # saying so lets a consumer tell the two apart.
            beyond = (max(0, -(-total_available // fullest) - parser_page_cap)
                      if fullest else 0)
            if beyond:
                logger.warning(
                    "This query is %d page(s) deeper than the site will "
                    "address. It caps every query at 6,750 results — 150 "
                    "pages of 45 — however many it matched, and states that "
                    "itself. This site publishes no page 2 at any url "
                    "does not fail: it redirects to page 1 and serves it "
                    "with HTTP 200. Narrow the query with the site's own "
                    "— use --symbols to read more instruments, and run each "
                    "slice.", beyond)


    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column,
    # and IDENTICAL to the other two engines — a sidecar that differs between
    # engines is the drift `finish_run()` exists to prevent, one level up.
    #
    # This carried a sibling's keys (`scroll`, `result_header`,
    # `pages_still_growing`) until an audit diffed the three engines' actual
    # sidecars: `scroll` is meaningless here (this site serves its whole page
    # at once) and the cap arithmetic was MISSING, which is the figure that
    # keeps `status: complete` honest on a site that serves 6,750 of
    # 3,000,000 matches.
    # Always recorded, never conditional. `market` is what makes two runs
    # comparable or not: the root page's lists are geo-selected, so a gl=US
    # run and a gl=DE run are two SAMPLES rather than a before and an after,
    # and diff_runs.py reads this to refuse the comparison instead of
    # reporting every row as added and removed.
    #
    # `capped_by_site` is True on every run here and means the OPPOSITE of
    # what it means in the sibling this field came from. There it meant "the
    # site will serve you 15 of 1,268 pages, so a complete run is a 1.2%
    # sample". Here it means one page IS everything the site publishes for
    # this url, so a one-page run is exhaustive. The sidecar says which,
    # through `paginates: false` beside it, and no warning is logged —
    # warning on every healthy run is how a reader learns to ignore
    # warnings.
    extra = {
        "market": args.market or market_from_url(args.url),
        "language": (args.locale or "en").split("-")[0],
        "listing_kind": listing_kind(args.url),
        "paginates": False,
        "capped_by_site": True,
        "reachable_max": 1,
        "symbols_requested": len(getattr(args, "_symbol_urls", []) or []),
    }
    if args.category:
        extra["strip_filter"] = args.category
    headers = {o.page_num: o.header for o in outcomes if o.header}
    if headers:
        extra["page_language"] = headers

    # What happened to each unit of work, recorded rather than left in the
    # log. A run of ten symbols with three typos returns seven rows, and
    # without these three numbers the only way to notice is to count the
    # rows yourself and know what you asked for.
    #
    # `not_found` is deliberately NOT a failure: Google answered, and the
    # answer was "there is no such instrument". A run whose only shortfall
    # is misspelt symbols is `complete` and exits 0 — the symbols that
    # FAILED (a load error, a block, a regional refusal) are the ones that
    # make it partial, and those already land in `pages_failed`.
    _states = [getattr(o, "state", None) for o in outcomes]
    extra.update({
        "symbols_requested": len(getattr(args, "_symbol_urls", []) or []),
        "symbols_ok": sum(1 for o in outcomes if o.ok and o.products),
        "symbols_not_found": sum(1 for s in _states if s == "not_found"),
        "symbols_failed": sum(1 for o in outcomes if not o.ok),
    })
    _missing = extra["symbols_not_found"]
    if _missing:
        logger.warning(
            "%d of %d symbol(s) do not exist on Google Finance; the run is "
            "complete and those rows are simply absent. The sidecar records "
            "the counts.", _missing, extra["symbols_requested"])

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      # `google.com/finance`, matching every ROW's `source` — a run
                      # can start on www.google.com/finance and
                      # finish on search.google.com/finance,
                      # because page_url rewrites a listing
                      # landing page onto the search host.
                      source=SOURCE_DEFAULT,
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Google Finance scraper (pyppeteer edition). pyppeteer is "
                    "effectively unmaintained — playwright_scraper.py is the "
                    "primary engine.")
    p.add_argument("--url", default=None,
                   help="A Google Finance URL. In --mode quote: one "
                        "instrument page "
                        "(www.google.com/finance/quote/GOOGL:NASDAQ). In "
                        "--mode markets or --mode movers: the Google Finance "
                        "root (www.google.com/finance/), whose lists are "
                        "what those modes read. A legacy /finance/... URL is "
                        "rewritten to the /finance/beta/... the site "
                        "redirects it to, and the seven /finance/markets/* "
                        "URLs all redirect to the root, so passing one of "
                        "those is accepted and reads the root. Optional: "
                        "--symbols builds quote URLs for you, and the list "
                        "modes default to the root. GOOGLE_FINANCE_URL in "
                        "the environment or in .env works too.")
    p.add_argument("--symbols", default=None, metavar="SYM[,SYM...]",
                   help="Comma-separated instruments for --mode quote — "
                        "GOOGL:NASDAQ,BMW:ETR,EUR-USD. This is the unit of "
                        "work on this site: Google Finance does not "
                        "paginate, so a run fetches one page per SYMBOL "
                        "rather than N pages of one listing, and "
                        "--concurrency splits these rather than pages. "
                        "Venue-traded instruments are TICKER:EXCHANGE and "
                        "currency or crypto pairs are BASE-QUOTE (EUR-USD, "
                        "BTC-USD) — the colon form of a pair is the trap, "
                        "because EURUSD:CURRENCY returns HTTP 200 with the "
                        "site's own Page Not Found and echoes the ticker "
                        "back at you.")
    p.add_argument("--market", default=None, metavar="CC",
                   help="Two-letter market to read, sent as Google's own "
                        "`gl` parameter (US, DE, GB, JP, IN...). This is "
                        "what makes a named market reachable WITHOUT a "
                        "proxy: measured 2026-09-22 from one Helsinki "
                        "datacentre address, gl=US returned NASDAQ movers "
                        "and CBOE sector indices, gl=DE returned ETR, and no "
                        "parameter at all returned the exit IP's own market. "
                        "Load-bearing for --mode markets and --mode movers, "
                        "whose lists are geo-selected; two markets are two "
                        "SAMPLES, not a change in the data, and diff_runs.py "
                        "refuses to compare across it.")
    p.add_argument("--mode",
                   choices=["quote", "markets", "movers", "financials",
                            "analysts", "earnings", "chart"],
                   default="quote",
                   help="INSTRUMENT MODES, one --symbols entry each. "
                        "quote (default): the current quote with the "
                        "session's open/high/low, volume, market cap and "
                        "industry. financials: one row per reporting "
                        "period, quarterly back to 2004 on a large cap — "
                        "revenue, net income, operating expense, EBITDA, "
                        "EPS, net margin, effective tax rate, and what the "
                        "street had estimated. analysts: the consensus "
                        "verdict, the buy/hold/sell split, the 12-month "
                        "target range, and every published analyst action "
                        "with its firm, date and price target. chart: every "
                        "OHLCV bar the page already carries — the latest "
                        "session at five-minute resolution AND about a "
                        "month of daily bars, in one run with no extra "
                        "fetch. "
                        "MARKET MODES, one page each. markets: every strip "
                        "the market page publishes — 46 rows on the "
                        "2026-09-22 gl=US capture, being 20 broad indices, "
                        "11 sector indices, 5 currency pairs, 5 crypto "
                        "pairs and 5 futures. movers: that page's gainers, "
                        "losers and most-active, which are PREVIEWS — "
                        "Google retired the standalone "
                        "/finance/markets/gainers pages and publishes only "
                        "the top few of each, so a full ranking is not "
                        "available from this site at any price. earnings: "
                        "its upcoming announcements calendar, with revenue "
                        "and EPS estimates.")
    p.add_argument("--category", default=None, metavar="STRIP",
                   help="Keep only rows from one of the market page's "
                        "strips in --mode markets — index, sector, "
                        "currency, crypto or futures — or one of gainers, "
                        "losers, most_active in --mode movers. These are "
                        "the listings the retired /finance/markets/* URLs "
                        "used to serve, so this is how to ask for just one "
                        "of them. Ignored in the instrument modes, which "
                        "read one named instrument.")
    p.add_argument("--pages", type=int, default=1,
                   help="Accepted for family compatibility and refused above "
                        "1, with the reason. Google Finance does not "
                        "paginate: a quote page is one instrument, and the "
                        "seven /finance/markets/* URLs that used to serve "
                        "gainers, losers, most-active, currencies, "
                        "cryptocurrencies, climate leaders and indexes ALL "
                        "redirect to the root, which carries its lists in "
                        "one payload. Honouring --pages 3 here would fetch "
                        "the same page three times, find no new symbol, and "
                        "report a complete three-page run that read one "
                        "page — which is the exact silent-success failure a "
                        "sibling shipped. Use --symbols to read more "
                        "instruments.")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for flag parity and IGNORED here: parallel "
                        "page fetching lives in playwright_scraper.py.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "A page that comes back EMPTY is not retried: an empty "
                        "hub category is a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="google_finance", help="Output file prefix")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999. "
                        "Credentials are sent over CDP (page.authenticate), "
                        "never on the browser's command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int,
                   default=page_flow.BLOCK_RETRIES_WITH_POOL,
                   help="When a page comes back refused, retry it from this "
                        "many OTHER exits before giving up (default %d, from "
                        "page_flow.BLOCK_RETRIES_WITH_POOL — the site's "
                        "measured policy lives there rather than in three "
                        "copies of a literal). Needs a pool of more than one; "
                        "ignored otherwise. Worth less on this site than on "
                        "most in this family, and that is measured: one "
                        "datacentre address was served the full catalogue by "
                        "plain curl AND by headless Chromium, and refused "
                        "only when a curl handshake claimed a Chrome "
                        "User-Agent. What gets refused here is an "
                        "inconsistent CLIENT, not an address."
                        % page_flow.BLOCK_RETRIES_WITH_POOL)
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). Its LANGUAGE half "
                        "is also sent to Google as the `hl` parameter, so "
                        "--locale de-DE asks for German labels. It does NOT "
                        "pick the market and it does not pick the currency: "
                        "`gl` does that, which is --market, and each row "
                        "carries the currency its own venue quotes in. "
                        "Measured 2026-09-22: `hl` changes the index and "
                        "sector NAMES and leaves every symbol, price and "
                        "venue byte-identical, so a locale run and an "
                        "English one diff on labels only.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's "
                        "Fingerprint API and apply it to the launched "
                        "browser. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint, where the Scraping Browser API "
                        "supplies its own — stacking a second identity on "
                        "top is how you manufacture the one thing Akamai "
                        "refuses here, a client that contradicts itself. On "
                        "its own a fingerprint is measured to work on this "
                        "site: the API supplies the client hints, timezone "
                        "and languages to go with the user agent, and such "
                        "a run was served 90 rows over two pages.")
    # ONE OS-family tag, not a list. It shipped as "Windows,Chrome,Desktop"
    # across this family, which the API rejects with HTTP 400, so
    # --fingerprint failed on every invocation while looking configured
    # (§17). Measured against the live API 2026-09-10: `Windows` succeeds;
    # `Windows,Chrome,Desktop`, `Chrome` and `Desktop` each 400.
    p.add_argument("--fp-tags", default="Windows", metavar="TAG",
                   help="ONE OS-family tag for the fingerprint filter "
                        "(default Windows). A LIST is rejected by the API "
                        "with HTTP 400, and so are Chrome/Desktop/Mobile "
                        "on their own.")
    p.add_argument("--fp-country", default=None, metavar="CC",
                   help="Two-letter country for the fingerprint filter.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Note that "
                        "NO challenge has ever been observed on this site — a "
                        "refused request gets no page at all — so neither "
                        "setting has anything to act on today, and neither "
                        "helps with a refusal.")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to a running browser over CDP, e.g. "
                        "ws://user:pass@host:port. pyppeteer authenticates on "
                        "the WebSocket upgrade, so a credentialed Scraping "
                        "Browser endpoint works here.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Browser executable to drive, instead of the Chromium "
                        "pyppeteer downloads for itself. Needed where that "
                        "build will not start: on an Apple Silicon Mac "
                        "pyppeteer fetches an x86_64 Chromium 117, which runs "
                        "under Rosetta far enough to print --version and then "
                        "fails to open its DevTools socket (measured "
                        "2026-09-08; the same failure occurs with no wrapper "
                        "code at all, so it is the build, not this engine). "
                        "Point it at a Chrome or Chromium of your own — "
                        "Playwright's, if you have it installed.")
    # HEADFUL by default, a departure from every sibling repo and a measured
    # one: Akamai refuses a headless browser here whatever the exit -- HTTP
    # 403 and a 394-byte "Access Denied" from four residential exits and one
    # datacentre address, against 200 and the full catalogue from those same
    # addresses with a real window.
    # HEADLESS is the default, and here that is measured in BOTH
    # directions rather than inherited (§19). On 2026-09-21 from one
    # datacentre address the answer was PER-ROUTE:
    #
    #   search / item / category   headless 200 with the payload
    #                              headful  200 with the payload
    #   ranking.google.com/finance      headless 403, 3 of 3
    #                              headful  200, 3 of 3
    #
    # So the routes this scraper reads do not care, and headless is right as
    # the default. One route on this site does care, and it is the one this
    # repo does not read.
    p.add_argument("--headful", dest="headless", action="store_false",
                   help="Run with a real browser window. Not needed for the "
                        "routes this scraper reads, which were served "
                        "headless in testing.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   default=True,
                   help="Run headless. THE DEFAULT. Ignored with "
                        "--cdp-endpoint, where the remote browser decides.")
    args = p.parse_args()
    env_config.apply(args)
    if args.fingerprint and not args.twocaptcha_key:
        p.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                "uses the same key, though it is a separate subscription "
                "from solving).")
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "remote browser brings its own identity, and stacking "
                       "a second one creates a contradiction rather than "
                       "better cover.")
        args.fingerprint = False
    if args.market:
        args.market = args.market.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", args.market or ""):
            p.error("--market takes a two-letter market code (US, DE, GB, "
                    "JP...); got %r." % (args.market,))

    if args.category:
        if args.mode == "earnings":
            p.error("--mode earnings has no strips to filter: its rows are "
                    "announcements, not entries in a list. Drop --category.")
        allowed = MARKET_STRIPS if args.mode == "markets" else MOVER_LISTS
        if args.mode in QUOTE_PAGE_MODES:
            logger.warning("--category is ignored in --mode %s, which "
                           "reads one named instrument rather than a list.",
                           args.mode)
            args.category = None
        elif args.category not in allowed:
            p.error("--category %r is not a strip of --mode %s. Pick one of: "
                    "%s." % (args.category, args.mode, ", ".join(allowed)))

    # Refused above 1, with the reason, rather than honoured as a no-op.
    # Google Finance does not paginate (see page_flow's module docstring),
    # so fetching "three pages" would fetch one page three times and report
    # a complete three-page run — the silent-success failure §7 is about.
    if args.pages and args.pages > 1:
        p.error("%s (got --pages %d)"
                % (no_pagination_reason(args.url or ""), args.pages))

    language = (args.locale or "en").split("-")[0] or None
    symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()]

    if args.mode in QUOTE_PAGE_MODES:
        if symbols and args.url:
            p.error("pass --symbols or --url, not both: --mode %s reads "
                    "the instruments you name, and two sources for that "
                    "list is a silent way to read the wrong one."
                    % (args.mode,))
        if symbols:
            seen = []
            for s in symbols:
                if s.upper() not in [x.upper() for x in seen]:
                    seen.append(s)
            if len(seen) != len(symbols):
                logger.warning("--symbols had %d duplicate(s); reading each "
                               "instrument once.", len(symbols) - len(seen))
            args._symbol_urls = [quote_url(s, args.market, language)
                                 for s in seen]
            args.url = args._symbol_urls[0]
        elif args.url:
            args.url = with_market(canonical_url(args.url), args.market,
                                   language)
            if not symbol_from_url(args.url):
                p.error("--mode %s needs an instrument URL "
                        "(www.google.com/finance/quote/GOOGL:NASDAQ); %r "
                        "addresses no instrument. Use --mode markets, "
                        "movers or earnings for the market page, or pass "
                        "--symbols." % (args.mode, args.url))
            args._symbol_urls = [args.url]
        else:
            p.error("--mode %s needs --symbols (GOOGL:NASDAQ,BMW:ETR) or "
                    "an instrument --url, and GOOGLE_FINANCE_URL is not "
                    "set in the environment or in .env." % (args.mode,))
    else:
        if symbols:
            p.error("--symbols names instruments and --mode %s reads the "
                    "market page; the two do not combine. Use --mode "
                    "quote, financials, analysts or chart for named "
                    "instruments." % (args.mode,))
        args.url = (with_market(canonical_url(args.url), args.market, language)
                    if args.url else markets_url(args.market, language))
        if symbol_from_url(args.url):
            p.error("%r is one instrument and --mode %s reads the market "
                    "page. Use --mode quote, financials, analysts or chart "
                    "for it." % (args.url, args.mode))
        args._symbol_urls = [args.url]

    if not is_supported_host(args.url):
        # Refused rather than attempted. This parser reads Google Finance's
        # own payload shape; pointed at another site it would not fail
        # loudly, it would return zero rows and look like an empty market.
        p.error(unsupported_reason(args.url)
                or "%r is not a Google Finance URL this scraper reads."
                % (args.url,))

    # The family's page loop iterates `args.pages`, and on this site the
    # unit of work is the SYMBOL — one url per instrument, never a page 2 —
    # so the symbol count IS the fetch count. Setting it here lets the whole
    # shared loop, the sidecar's `pages_completed`, the per-page failure
    # list and the concurrency dispatcher work unchanged, and keeps `page`
    # on a row meaning "which fetch produced this", which is what a consumer
    # needs. The user-facing --pages flag stays refused above 1 above: it
    # would mean "fetch this url N times".
    args.pages = len(args._symbol_urls)
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except Exception as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest
        # one: `profile_locked` means another run still holds this `pid`, and
        # a harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
