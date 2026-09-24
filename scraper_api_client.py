#!/usr/bin/env python3
"""google-finance-scraper — 2captcha Scraper API edition (fourth engine)

One HTTP request per page, no local browser, no Playwright install. The
2captcha Scraper API fetches the page from its own infrastructure and returns
the HTML; this client parses it with the same `product_parser` the browser
engines use, so the rows and columns are identical.

MEASURED ON THIS SITE, 2026-09-21
---------------------------------
It works, and it is the cheapest way in:

    POST https://scraper.2captcha.com/tasks/sync
    -> HTTP 200, 1,092,737 bytes of HTML, 45 products parsed, $0.0005

Its 52-entry payload carried 7 sponsored placements, which the parser
dropped with a log line — the same behaviour as every other transport, which
is the point of this client sharing `product_parser` rather than having its
own.

This section previously said the path was unverified, because no key was
available to the session that wrote the repo. That was the honest sentence at
the time and it is worth saying why it was phrased that way: a sibling repo
once shipped a README telling readers a 2Captcha key would not help them,
when the truth was that the client had not implemented the task type — a
claim about the PRODUCT made from a fact about the CODE, invisible to every
test, and wrong in the direction that costs a reader money. So the rule is
that an unrun path says "not run", never "does not work".

WHAT TO KNOW BEFORE USING IT HERE
---------------------------------
* The gate on this site is the CONSISTENCY of the client, not the address.
  Measured from one datacentre IP: plain curl with curl's own User-Agent was
  served 92 KB and 45 products; the same curl claiming a Chrome User-Agent
  got a 43-byte Akamai deny. This API's fetcher gets in, so whatever it
  sends is self-consistent — but that is a fact about the API today, not a
  guarantee, and it is exactly the kind of thing to re-measure rather than
  inherit.
* If it is ever refused, the refusal arrives under **HTTP 200** with a
  43-byte body. Neither this client nor the parser may trust the status
  code; both decide "was this served at all" from the reference-id shape and
  from the page being built out of Google Finance's own asset hosts.
* One request is one page. There is no pagination here — pass `?p=N`
  yourself, and mind that Google Finance caps every query at 6,750 results.

    python3 scraper_api_client.py \\
        --url "https://search.google.com/finance/search/mall/コーヒー/"

    # $TWOCAPTCHA_KEY is read from the environment or .env, so a key never
    # has to be typed — a secret in argv is readable by anything that can run
    # `ps` and lands in shell history.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from typing import Optional

import requests

from product_parser import (parse_products, quote_url, markets_url,
                            with_market, canonical_url,
                            market_from_url, QUOTE_PAGE_MODES,
                            MARKET_PAGE_MODES, detect_bot_challenge,
                            detect_page_state)
from output_writer import save
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# Exit codes. Kept distinct from 2 (bad usage) on purpose: a remote API
# failing is not the operator passing wrong arguments, and a harness that
# lumps them together sends you looking in the wrong place. An early run
# reported `exit=2` for an HTTP 422 from the API — which reads as "you called
# it wrong".
#
# Imported rather than redefined: the browser engines return the same code for
# a Scraping Browser that will not accept a connection, and two definitions
# of one exit code is how a family's contract drifts.
from output_writer import EXIT_API_ERROR  # noqa: E402

def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


# Credentials embedded ANYWHERE in a blob of text, not just in a string that
# is entirely a URL — and every occurrence, not the first. A masker that
# handles one occurrence prints the password the other four times and looks
# like it is working.
_CREDS_IN_TEXT_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s'\"@]+@", re.IGNORECASE)
# Same shape as captcha_solver's and fingerprint_client's. A third copy is
# one too many and they should be unified in a family pass; reaching into
# another module's private name to avoid it would be worse.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact_debug_header(value: str) -> str:
    """The x-debug header, safe to log.

    SECURITY.md names this header as one of three places credentials reach a
    log unmasked, and it was logged verbatim: the API echoes back the task it
    ran, so a run driven through a credentialed CDP endpoint put that
    endpoint's username and password into the log, and a key passed as a
    query parameter would go the same way.

    Redaction rather than an allowlist of fields, deliberately: the header is
    the API's own metadata and its shape is not ours to pin, so an allowlist
    would silently drop the cost and timing figures this is logged FOR the
    first time the API adds a field.
    """
    return _KEY_IN_TEXT_RE.sub(r"\1=***",
                               _CREDS_IN_TEXT_RE.sub(r"\1***:***@", value))


def _build_wait_for(args) -> Optional[dict]:
    """`waitFor` is sent as a JSON OBJECT. Measured 2026-09-23: the
    JSON-encoded-string form this client used before is refused with HTTP
    422 ("params.waitFor must be an object") and still billed; the object
    form answers 200.

    Default (no flag): wait for the DOM. On a challenge-protected page
    that resolves instantly against the challenge page itself — which is
    exactly the trap documented in this module's docstring, so
    --wait-text/--wait-element exist to wait on something only the real
    page can contain."""
    if args.wait_text:
        return {"text": args.wait_text}
    if args.wait_element:
        return {"element": args.wait_element, "checkVisible": True}
    if args.wait_state:
        return {"state": args.wait_state}
    return None


def _target_status(body: dict) -> Optional[int]:
    """The TARGET page's HTTP status, or None.

    Measured 2026-09-23: the response carries the target's code as
    `http_code` (an int) and the API's own verdict as `status` ("success").
    Reading `status` handed the string "success" onward and a target 403
    was never seen. `status` is kept as a fallback only when it is an int,
    for the older response shape. bool is excluded: it is an int to Python.
    """
    for key in ("http_code", "status"):
        v = body.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


def fetch_html(args) -> str:
    payload = {
        "task_type": "scrape",
        "url": args.url,
        "data_format": "raw",   # we want HTML; product_parser does the rest
        "format": "json",       # so we get {"status", "http_code", "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }

    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", json.dumps(wait_for))

    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, args.url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header — worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", _redact_debug_header(debug))

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces: "CDP connect failed (user cdpurl) after N
        # attempts"); 402 = out of balance; 408 = sync wait exceeded.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    html = body.get("body") or ""
    upstream_status = _target_status(body)
    logger.info("Upstream page status %s, %d bytes of HTML.", upstream_status, len(html))
    # The STATUS is returned alongside the HTML, not thrown away. It used to
    # be, and that cost this engine the family's central distinction. On this
    # site a refusal carries no markup at all — nothing a challenge check
    # on it, so the challenge check below finds nothing and the run fell
    # through to "0 products" and exit 4. A pipeline branching on the exit
    # code then reads a block as an empty category. See detect_page_state,
    # which the three browser engines already reach through page_flow.
    return html, upstream_status


def main() -> int:
    args = parse_args()

    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2

    # A challenge page is not necessarily final (see _run_once), so a
    # single attempt is not evidence. Each retry is a fresh billable task —
    # $0.0005 at the observed rate — so the default is deliberately low.
    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        rc = _run_once(args, attempt, attempts)
        if rc != 3 or attempt == attempts:
            return rc
        logger.info("Challenge page on attempt %d/%d — retrying in %ds.",
                    attempt, attempts, args.retry_delay)
        time.sleep(args.retry_delay)
    return rc


def _run_once(args, attempt: int = 1, attempts: int = 1) -> int:
    if attempts > 1:
        logger.info("Attempt %d/%d", attempt, attempts)

    try:
        html, upstream_status = fetch_html(args)
    except requests.RequestException as e:
        logger.error("Network error talking to the Scraper API: %s", e)
        return EXIT_API_ERROR
    except RuntimeError as e:
        # HTTP 4xx/5xx from the API, including the 422 that a busy or
        # unreachable cdpurl produces.
        logger.error("%s", e)
        return EXIT_API_ERROR

    if args.dump_html:
        with open(args.dump_html, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Raw HTML written to %s", args.dump_html)

    # Same policy as the browser engines: the status decides the blocked
    # case, because this site's refusal has no marker to detect.
    state = detect_page_state(html, status=upstream_status, url=args.url)
    if state == "blocked":
        dump = f"{args.out}_scraperapi_debug.html"
        with open(dump, "w", encoding="utf-8") as f:
            f.write(html)
        logger.error(
            "The site did not serve the Scraper API's request (upstream "
            "HTTP %s, %d bytes) — saved to %s. Measured 2026-09-10: the "
            "Scraper API's own exit is a datacentre address, and this site "
            "answers those with NOTHING, while the same task routed through "
            "a Scraping Browser session returned 200 and 417 KB. Pass "
            "--cdp-url. This is exit 3, distinct from an empty result "
            "(exit 4).", upstream_status, len(html), dump)
        return 3

    vendor = detect_bot_challenge(html)
    if vendor:
        logger.error(
            "The Scraper API returned a %s bot-challenge page (%d bytes), not real content.",
            vendor, len(html),
        )
        logger.error("A challenge page is not a final answer — retry before concluding "
                     "anything (--retries). This site needs a rendered browser in the "
                     "path: pass --cdp-url, or use playwright_scraper.py / "
                     "puppeteer_scraper.py directly.")
        return 3

    market = args.market or market_from_url(args.url)
    products = parse_products(html, args.url, page=1, mode=args.mode,
                              market=market)
    if args.category and args.mode in MARKET_PAGE_MODES:
        products = [r for r in products if r.listing == args.category]
    logger.info("Parsed %d products.", len(products))

    if not products:
        dump = f"{args.out}_scraperapi_debug.html"
        with open(dump, "w", encoding="utf-8") as f:
            f.write(html)
        logger.warning("0 products parsed — saved the raw response to %s so you can see "
                       "what actually came back.", dump)
        return 4

    return save(products, args.out, args.format, allow_empty=args.allow_empty)


def parse_args():
    p = argparse.ArgumentParser(
        description="Google Finance scraper — 2captcha Scraper API edition (no "
                    "local browser). NOTE: these pages DO need JavaScript, "
                    "and their grid hydrates only as the page is scrolled, "
                    "so a single fetch returns about 5 products where a "
                    "browser engine returns 60. Pass --cdp-url to reach the "
                    "site at all; see this file's docstring for the measured "
                    "numbers, and prefer playwright_scraper.py.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var. A key passed on the
    # command line is visible to anyone who can run `ps`, and it lands in
    # shell history and in any log that echoes the command line.
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Defaults to $TWOCAPTCHA_KEY, which is the safer way to pass it.")
    p.add_argument("--url", default=None,
                   help="A Google Finance URL. Optional: --symbols builds "
                        "quote URLs for you, and the market modes default "
                        "to the site's root. GOOGLE_FINANCE_URL works too.")
    p.add_argument("--symbols", default=None, metavar="SYM[,SYM...]",
                   help="Comma-separated instruments for the instrument "
                        "modes — GOOGL:NASDAQ,BMW:ETR,EUR-USD. One request "
                        "per symbol, which is what this site's unit of work "
                        "is.")
    p.add_argument("--market", default=None, metavar="CC",
                   help="Two-letter market, sent as Google's own `gl`. It "
                        "selects WHICH market's data you get; it does not "
                        "get you past a regional refusal.")
    p.add_argument("--mode",
                   choices=["quote", "markets", "movers", "financials",
                            "analysts", "earnings", "chart"],
                   default="quote",
                   help="The same seven modes the browser engines take, "
                        "reading the same payload through the same parser.")
    p.add_argument("--category", default=None, help="Label to tag output rows with. Defaults to the category segment of the URL, so the column is never empty just because the flag was omitted.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="google_finance_scraperapi", help="Output file prefix")
    p.add_argument("--timeout", type=int, default=60,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 60)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over CDP "
                        "(sent as the API's `cdpurl` param), e.g. ws://user:pass@host:port")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page, e.g. '$'. Use this "
                           "on protected sites — a DOM/load wait is satisfied instantly by "
                           "the challenge page itself.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible, e.g. 'a[href*=\"-item-\"]'")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 products were parsed. Off by "
                        "default so a failed fetch can't overwrite a good result.")
    p.add_argument("--retries", type=int, default=1,
                   help="Extra attempts if a bot-challenge page comes back. One retry is "
                        "usually worth it. Each attempt is a separate billable task, so "
                        "this defaults to 1.")
    p.add_argument("--retry-delay", type=int, default=10,
                   help="Seconds between retries (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the raw returned HTML to this path (always, even on success)")
    args = p.parse_args()
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the env mapping is spelled out instead of defaulted.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "GOOGLE_FINANCE_CDP_ENDPOINT": "cdp_url",
        "GOOGLE_FINANCE_URL": "url",
    })
    args.market = (args.market or "").strip().upper() or None
    language = (getattr(args, "locale", None) or "en").split("-")[0]
    symbols = [x.strip() for x in (args.symbols or "").split(",") if x.strip()]
    if symbols and args.url:
        p.error("pass --symbols or --url, not both.")
    if args.mode in QUOTE_PAGE_MODES:
        if symbols:
            args._symbol_urls = [quote_url(x, args.market, language)
                                 for x in symbols]
            args.url = args._symbol_urls[0]
        elif args.url:
            args.url = with_market(canonical_url(args.url), args.market,
                                   language)
            args._symbol_urls = [args.url]
        else:
            p.error("--mode %s needs --symbols or an instrument --url, and "
                    "GOOGLE_FINANCE_URL is not set." % (args.mode,))
    else:
        if symbols:
            p.error("--symbols names instruments and --mode %s reads the "
                    "market page." % (args.mode,))
        args.url = (with_market(canonical_url(args.url), args.market, language)
                    if args.url else markets_url(args.market, language))
        args._symbol_urls = [args.url]
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
