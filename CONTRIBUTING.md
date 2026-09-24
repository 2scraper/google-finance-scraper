# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Google Finance changing its markup is the normal way this stops working, and it has
its own issue template. The detail that saves the most time is WHICH source
broke, because this scraper reads the site's own SSR payload rather than its
DOM, and there are two different payloads:

1. **The listing payload.**
   `window.__INITIAL_STATE__.state.data.the payload` — `items` (45 a page)
   and `pagination` (`numFound`, `start`, `pageSize`, `subset`). If this key
   moves, a run reports 0 rows and exit 4, which is loud, and the DOM
   fallback recovers `url`/`sku`/`title`/`price` and nothing else.
2. **The detail payload.** `<script type="application/json"
   id="item-page-app-data">` -> `newApi.itemInfoSku` (with `api.data.itemInfoSku`
   as the fallback). Its `sku[]`, `purchaseInfo` and `itemReviewInfo` carry
   the variant prices, the verified was-price and the aggregate rating.
3. **The currency.** A listing page states it once, in its own JSON-LD, on
   **page 1 only**. If `currency` goes null across a whole run, that block
   moved — not the prices.

Three things about this site that look like bugs and are not, so please check
them before filing:

* **The JSON-LD on a listing page is NOT the grid.** It is a ten-item SEO
  carousel; every url in it carries `?scid=seo-carousel-search`. A patch that
  "fixes" the parser to read it would return ten rows of the wrong products.
* **`original_price` is null on every listing row.** The listing payload
  publishes no was-price at all (0 occurrences across 405 measured rows). A
  verified one exists on the detail page, gated on Google Finance's own
  `doublePrice.referencePriceVerified` flag.
* **`in_stock` is True on every listing row.** The search excludes sold-out
  products rather than marking them (405 of 405, including pages 80-150 of a
  large market page).

And two that ARE worth filing immediately, because they would mean the
defences moved:

* a run that collects the same page twice — the end-of-listing check reads
  the offset the server states, and Google Finance re-serves page 1 under HTTP 200
  rather than erroring;
* `position` values that are not 1..N contiguous — the market page injects
  slots into its own result list and the count varies between fetches, so
  positions count emitted rows rather than payload slots.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once by
   hand before anyone trusts the badge. This canary needs **no secret** and
   runs daily on a schedule, which is deliberate: the README's central claim
   is that you need no key, no proxy and no account to read this site's
   listings, and a scheduled, ungated, real three-page run is that claim
   under test every morning. The family's rule that a canary which cannot
   pass must SKIP has a second half — a canary that CAN pass without a
   credential must never be gated on one, or the badge goes green every day
   while testing nothing.

   Note that `workflow_dispatch` needs the workflow to exist on the DEFAULT
   branch: from a feature branch `gh workflow run` answers
   `HTTP 404: workflow canary.yml not found on the default branch`, which
   reads like a typo in the filename. So the order is forced — merge first,
   dispatch second.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions; its fixtures live beside it in
`fixtures_generated.json` because on this site a fixture IS the SSR payload
and one ad's entry is kilobytes of JSON (see `make_fixtures.py`). Copy the
nearest existing check and edit it.

The properties below exist because they were once absent, or were wrong in
the repo this one was ported from, and cost real time. Tests pin all of them,
so a PR that breaks one will fail rather than silently regress:

- **The SSR payload is the primary source, and JSON-LD is a TRAP.** A
  listing page's only `application/ld+json` is a ten-item SEO carousel whose
  every url carries `?scid=seo-carousel-search`, while the page holds 45
  products. It is read for exactly one thing: the currency, which no other
  part of a listing page states.
- **A detail page publishes a different structure entirely** — an
  `item-page-app-data` island and a JSON-LD `BreadcrumbList`. The listing
  parser finds nothing on it, which the suite asserts directly.
- **`sku` is Google's own canonical symbol** — `TICKER:EXCHANGE` for a
  venue-traded instrument and `BASE-QUOTE` for a pair — read out of the
  payload rather than rebuilt from the URL. The
  payload's `variantId` is the obvious candidate and is wrong: it names the
  pre-selected SKU inside the item and matches the URL's own code on only 39
  of 180 measured rows. The URL tail makes ONE id work on both routes, which
  is what lets a consumer join a listing run to a product run.
- **Every page is UTF-8**, and the parser still honours a declared
  charset rather than assuming one.
  `product_parser.decode_page` is the one place that knows; the browser
  engines never meet it, and an HTTP client that assumes UTF-8 either raises
  or produces a whole page of replacement characters while the numbers still
  parse.
- **`position` counts emitted rows, not payload slots.** Google Finance injects
  paid placements into its market lists — measured on one
  measured page, and the count varies between fetches — so numbering by slot
  made the same 45 products come out 1-45 in one engine and 8-52 in another.
- **Zero is not a rating.** An unreviewed product comes back
  `{score: 0, numReviews: 0}`, on 28 of 405 measured rows; both columns are
  nulled together, keyed on the COUNT.
- **A was-price is read only where the site says it may be.**
  `doublePrice.referencePriceVerified` is Google Finance's own outcome of Japan's
  double-pricing substantiation rule. Verified on 2 of 8 measured item
  pages, absent with the flag false on the rest.
- **The end of a listing is the offset the SERVER states.** Google Finance does not
  error past its last page: `?p=151` of a 150-page query redirects to page 1
  and serves it with HTTP 200, and a `/category/{genreId}/` URL serves page 1
  for any `?p=` at all. Both look like success, so every fetched page is
  checked against `pagination.start`.
- **Complete and exhaustive are different words.** Every query is capped at
  6,750 results — 150 pages of 45 — however many it matched, and the site
  states both numbers. A full run of a 3-million-hit query is complete and is
  a 0.2% sample; the sidecar carries both figures.
- **A block here is the CLIENT, not the address.** One datacentre IP was
  served the full catalogue by plain curl AND by headless Chromium, and
  refused with a 43-byte deny only when a curl handshake claimed a Chrome
  User-Agent. So `--headless` stays the default, the no-pool block budget is
  1 rather than 3, and the block message talks about fingerprints before
  exits.
- **A refusal can be HTTP 200**, and there are three skins of it that do not
  agree on a status: the 43-byte Akamai deny (200), the branded page under
  403, and the SAME branded page under 503, which is a rate-limit throttle
  rather than a refusal. Only the status separates the last two, and only the
  body separates the first — so the classifier needs both and each covers the
  other's blind spot.
- **A UA override is what broke the pyppeteer engine**, and it is the shape
  to watch for: served on nav 1, denied on navs 2-4. If you add a user agent
  or a fingerprint to any engine, test it over TWO pages.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
google.com/finance, say in the PR what you ran, which URL and page kind, from
which exit, and what you got — including the price and image coverage
percentages the run prints, and the scroll trace from the sidecar. Note that
a run from a datacentre address gets NO RESPONSE AT ALL, so "it returned
nothing" from a VPS is not a finding. Product counts differ by category, by
URL and by how far the scroll got, so a bare "worked for me" is not
reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: the first live run of the pyppeteer engine crashed on its
FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

Do not add anything that submits the registration form. This project
deliberately never does, and a captcha token proved valid by creating a real
account is not a result worth having.

## Scope

This repo scrapes **public pages** on Google Finance: genre listings, search
listings and product pages, exactly as an anonymous visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
