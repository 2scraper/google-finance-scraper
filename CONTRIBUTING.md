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
part broke. This scraper reads no DOM for data and no JSON-LD (there is none,
0 blocks across 11 captures): every row comes out of the
`AF_initDataCallback({key: 'ds:N', data: [...]})` blobs in the first
response, found by the SHAPE of each record rather than by its `ds:N` key,
because the key numbering shifts the moment a query parameter changes the
page.

Things about this site that look like bugs and are not, so please check them
before filing (all in the README):

* **`movers` returns a preview**, the top 1 to 4 of each list. Google
  retired the standalone gainers/losers/most-active pages; a full ranking is
  not published.
* **`--window` does not deepen `--mode chart`.** Every value returns the same
  ~20 daily bars plus one intraday session.
* **`eps` is null on some instruments** — 0 of 88 periods on `7203:TYO` —
  because Google publishes none in that slot, and it is deliberately not
  filled from the adjacent basic-EPS slot.
* **`EURUSD:CURRENCY` reports `not_found`.** Pairs are spelled `EUR-USD`; the
  colon form returns HTTP 200 with Google's own Page Not Found.

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
   is that you need no key, no proxy and no account to read this site, and a
   scheduled, ungated, real run across four symbols is that claim under test
   every morning. The family's rule that a canary which cannot pass must SKIP
   has a second half — a canary that CAN pass without a credential must never
   be gated on one, or the badge goes green every day while testing nothing.

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
`fixtures_generated.json` because on this site a fixture IS the page payload
and one page is kilobytes of nested JSON (see `make_fixtures.py`). Copy the
nearest existing check and edit it.

The properties below were each measured, and tests pin them, so a PR that
breaks one will fail rather than silently regress:

- **Never anchor on `ds:N`.** The suite renumbers every key in a real fixture
  and asserts no row changes.
- **A Not Found page is read before rows are counted.** Its page chrome still
  carries index and sector quotes, so "did we get rows?" answers yes on a page
  holding no such instrument.
- **The gate is the CLIENT, and here a browser User-Agent is REQUIRED.** curl
  with its own UA is redirected to `/finance/beta/unsupported`; a Chrome UA
  gets the real page. That state is `unsupported_client`, never a block,
  because no proxy or solve would change it.
- **`--pages` above 1 is refused with the reason.** The site does not
  paginate; `--concurrency` splits symbols, not pages.
- **`financials` exposes seven identified slots and no others**, and the
  analyst split is checked against the stated total before it is written.

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

Most do not — the suite covers the parser, the writers, the page classifier
and the CLI contract against real fixtures. If yours genuinely needs
google.com/finance, say in the PR what you ran — which mode, symbols and
`--market`, from which exit — and what you got, including the row count and
the `state` in the sidecar. The market page's lists are geo-selected, so a
count without its `--market` is not reproducible.

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
