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

Google Finance changing its payload is the normal way this stops working, and
it has its own issue template. The detail that saves the most time is WHICH
record broke. The scraper reads no DOM and no JSON-LD (there is none on this
site): every row comes out of the `AF_initDataCallback({key: 'ds:N', data:
[...]})` blobs in the first response, and each mode reads a different record
inside them, recognised by its SHAPE rather than by its `ds:N` key:

1. **The quote record** (`--mode quote`, and the rows of `markets` and
   `movers`): symbol, price, previous close, day range, volume, market cap.
2. **The quarterly record** (`--mode financials`): 106 unlabelled numbers per
   quarter, of which seven are read.
3. **The analyst record** (`--mode analysts`): the consensus, the buy/hold/sell
   split, the target range and each published action.
4. **The bar records** (`--mode chart`): OHLCV, ordered open, close, high, low.
5. **The earnings calendar** (`--mode earnings`): one entry per announcement,
   with the street's revenue and EPS estimates.

A run that reports `complete` while a column that used to be filled is null
on every row usually means one of those records changed shape. Attach the
`--dump-html` output; the suite's fixtures are cut from exactly those bytes.

Three things about this site that look like bugs and are not, so please check
them before filing:

* **`movers` returns a preview, not a ranking.** The market page publishes the
  top one to four of each list; the standalone ranking pages redirect to it.
* **`market_cap` and `volume` are null on indices, currencies, crypto and
  futures.** Google writes `0` there to mean "there is none".
* **`--window` does not deepen the chart.** Every window returns the same
  ~20 daily bars plus one session of 5-minute bars.

And two that ARE worth filing immediately:

* any mode whose row count or non-null share drops on an unchanged symbol;
* a wrong symbol that comes back as rows instead of `not_found`.

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
   runs weekly (Mondays) on a schedule, which is deliberate: the README's central claim
   is that you need no key, no proxy and no account to read this site, and a
   scheduled, ungated, real run of every mode is that claim under test every
   Monday. The family's rule that a canary which cannot
   pass must SKIP has a second half — a canary that CAN pass without a
   credential must never be gated on one, or the badge goes green on every run
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
`fixtures_generated.json` because on this site a fixture IS the page's
payload and one capture is hundreds of kilobytes (see `make_fixtures.py`). Copy the
nearest existing check and edit it.

The properties below exist because they were once absent, or were wrong in
the repo this one was ported from, and cost real time. Tests pin all of them,
so a PR that breaks one will fail rather than silently regress:

- **The payload is found by SHAPE, never by `ds:N`.** The key numbering
  shifts when a query parameter changes the page (`?window=5D` moves every
  key up by one). The suite renumbers every key in a real fixture and
  asserts that no row changes.
- **`sku` is Google's own canonical symbol** — `TICKER:EXCHANGE` for a
  venue-traded instrument and `BASE-QUOTE` for a pair — read out of the
  payload rather than rebuilt from the URL, because a URL holds whatever was
  typed.
- **A positional figure is mapped only after it matched the rendered page on
  several instruments.** The financial record is unlabelled; a slot that
  matched one company only is a coin toss, so it is left out rather than
  guessed. Volume is not headcount, and OHLCV bars are open, close, high, low.
- **Zero is not a measurement.** Google writes `0` for "there is none" on
  `market_cap` and `volume`; those are nulled, never written through.
- **A wrong symbol is `not_found`, not an empty result.** Google answers an
  unknown symbol with HTTP 200 and a page whose chrome still carries dozens of
  real quotes, so the parser is scoped to the requested instrument.
- **The access axes are the client and the country.** A non-browser
  User-Agent is redirected to `/finance/beta/unsupported` (reported as its own
  state, never as a block); some countries get a 403 saying Finance is not
  supported in that region, which `--market` cannot get past. A real block is
  Google's interstitial for a badly scored address.
- **There is no pagination.** `--pages` above 1 is refused with the reason,
  and `--concurrency` splits symbols, not pages.

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
and the CLI contract against real fixtures. If yours genuinely needs
google.com/finance, say in the PR what you ran (mode, symbols, `--market`),
from which country, and what you got: row count and which columns were
null. Several figures move with the trading day (chart bars, movers), so a
bare "worked for me" is not reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: the first live run of the pyppeteer engine crashed on its
FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

## Scope

This repo scrapes **public pages** on Google Finance: quote pages and the
market page, exactly as an anonymous visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
