# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. A PATCH release means *fixes* — it does not promise that
every flag and default is frozen, and where a default changes in one, the
release notes lead with it.

## [Unreleased]

## [0.3.0] — 2026-10-15

> **Behaviour changes in this release:**
>
> - Output files are now written atomically (a temp file in the same
>   directory, then renamed). A new file gets the mode `open()` would have
>   given it under your umask; an existing file keeps its own mode.
> - In CSV output only, a text cell starting with `=`, `+`, `-`, `@`, a tab
>   or a newline is prefixed with `'` so a spreadsheet does not run it as a
>   formula. Numbers and the JSON output are unchanged; the sidecar's
>   `csv_cells_escaped` says how many cells were touched.
> - `.env.example` no longer sets `GOOGLE_FINANCE_URL` (it is shipped
>   commented out). A `GOOGLE_FINANCE_URL` in your own `.env` no longer
>   turns `--symbols` into a usage error; only a TYPED `--url` conflicts.
> - A region Google does not serve Finance in (HTTP 403, "not supported in
>   your region") is exit 3 with advice naming the exit's country; it used
>   to end as exit 4, which means "the page loaded and was empty".
> - A run whose loop ended normally while some symbols failed is `partial`
>   (exit 6), not `complete`.
>
> **Behaviour change:** `--retries 0` is now a usage error (exit 2). It used
> to be accepted and made the page-load loop unreachable, so nothing was
> fetched and the engine reported the browser's own blank page as a result:
> exit 3 or 4 with a 39-byte "capture", in tens of milliseconds. If a
> script of yours passes 0, pass **1**: the flag counts ATTEMPTS, so 1 means
> "try once and do not retry".

### Fixed

- **A failing captcha solver could be called up to four times for one page**
  against `SOLVES_PER_PAGE = 1`: the call before classification counted a
  solve only after it succeeded, so a solver error left the allowance
  unspent and each block retry called again. The allowance is now one
  object per page, spent before each purchase and never refilled by a retry
  or an exit rotation. Measured with a stubbed failing solver: 2 to 4 calls
  before, 1 after, with and without a proxy pool.
- **pyppeteer crashed (exit 1) on an ordinary navigation timeout with no
  proxy**: the load-failure branch rotated `pool`, which is None without
  `--proxy`. It now rotates only when a pool exists.
- **pyppeteer and Selenium classified markets, movers and earnings pages
  as `empty`** because they did not pass `--mode` to the classifier, so
  they skipped the readiness wait the Playwright engine runs. They pass it
  now; all three engines agree on the page state.
- **Writes were not atomic.** `open(path, "w")` truncates first, so a
  failure halfway through a write (full disk, kill) replaced the previous
  good `out.json` with a fragment of invalid JSON. JSON, CSV and the
  `.meta.json` sidecar now go through one atomic writer.
- **Text describing other sites was removed** from the issue templates,
  CONTRIBUTING, `diff_runs.py --help`, the `--headful` help, the blocked-page
  log message (which recommended an exit in another site's country) and the
  pyppeteer/Selenium market-run log, which printed a result-cap sample size
  this site does not have. Comments that cited private notes now state the
  reason in place.
- **`diff_runs.py` compared columns no row class here has**; it now derives
  the tracked fields from each mode's row class, and keys rows on the mode's
  own unique key rather than on `sku` alone.
- **A typo in one symbol ended the whole queue**; the other symbols are now
  fetched and the bad one is reported as `not_found`.
- **`--retries 0` fetched nothing and called it a result.** The load loop is
  `range(1, retries + 1)`, so 0 never runs it and the engine judged
  `about:blank`. Three third-party audits of this family ran exactly that and
  reported a working scraper as broken. `--retries` now goes through
  `page_flow.attempts` in every entry point; a check reads the source of each
  one and calls the validator, so a new engine declaring the flag with a
  plain int fails the suite.
- **Scraper API: `waitFor` is now sent as a JSON object.** Measured
  2026-09-23 against `scraper.2captcha.com/tasks/sync`: the JSON-encoded
  string this client sent (the form older docs described) is refused with
  HTTP 422 "params.waitFor must be an object" -- and the task is still
  billed ($0.0005) -- so every run with `--wait-text` or its sibling wait flags
  failed with exit 5. The object form answers HTTP 200.
- **Scraper API: the target page's status is read from `http_code`.** The
  response's `status` field is the API's own verdict string (`"success"`),
  not the target site's HTTP code, so a target 403/503 was never seen by
  this client. `http_code` (an int) is read now, with `status` kept as a
  fallback only when it is an int.

### Fixed — Google Finance changed its payload

- **`--mode earnings` returned every event with `revenue_estimate`,
  `eps_estimate`, `currency` and `period_end` null since 2026-10-09**, while
  the run reported `complete` (the canary caught it). The gl=US calendar now
  sends each event's figures in Google's sparse array encoding — a 19-slot
  list ending in a `{"108": ..}` object for an event already reported, or a
  lone `{"9": .., "11": .., "17": .., "18": ..}` object for one not yet
  reported — and the parser accepted only the dense 18/106-slot widths. It
  now expands the sparse form (field N is slot N-1, so the existing slot map
  applies unchanged) and recognises a figure record by its currency and
  period-end slots rather than by its exact width. gl=DE still sends the
  dense form; both are pinned by fixtures cut from 2026-10-14/15 pages.
- **`--mode movers` returned 0 rows (exit 4) whenever Google sent one of
  the three lists as null** — before the US open (`[null, null, [4
  most-active]]`, 2026-10-15) and early in a session (gl=DE had no gainers
  yet). The lists that are present are read now; the gainers/losers labels
  are checked on whichever of the two is present and fall back to
  positional names (`movers_N`) when neither is. Those most-active rows no
  longer leak into `--mode markets` either.
- **`--mode financials` dropped every detailed quarter of a bank.** BAC:NYSE
  publishes its detailed quarters 105 slots wide, not 106, so its newest row
  was 2024 Q2. They are read now (8 detailed quarters); the 2026 Q2 and Q1
  revenue and operating expense match the page's own rendered table.

## [0.2.0] — 2026-09-22

Four more modes, all reading the SAME first response the quote modes already
fetch. The site's Overview / Analysis / Earnings / Financials tabs are
rendered client-side from data that has already arrived, so
`--mode financials` costs one page fetch — the same one `--mode quote`
makes.

### Added

- **`--mode financials`** — one row per reporting period. 90 quarters back
  to 2004 on a large cap, with revenue, net income, operating expense,
  EBITDA, EPS, net profit margin, effective tax rate, and what the street
  had estimated for revenue and EPS.
- **`--mode analysts`** — the consensus verdict, the buy/hold/sell split,
  the 12-month target low/mean/high with Google's own upside figure, and
  every published analyst action with its firm, date, rating, price target
  and headline. 44 actions on `GOOGL:NASDAQ`.
- **`--mode chart`** — every OHLCV bar the page already carries: one full
  session at five-minute resolution AND about a month of daily bars, in one
  run. 99 bars on `GOOGL:NASDAQ`.
- **`--mode earnings`** — the market page's upcoming announcements, with
  revenue and EPS estimates. Geo-selected by `--market` like every other
  list on that page.
- Four row classes — `Financial`, `AnalystRating`, `EarningsEvent`,
  `ChartPoint` — each keeping the family's five-column prefix, so `sku`
  joins every mode to every other.

### Fixed

Three of these were found by running all three engines against the same URL
and diffing, which is the only reason any of them was visible:

- **The current session's daily bar was labelled a five-minute bar.** It
  carries the same timestamp as that session's LAST intraday bar, so
  grouping bars by date put a whole-day bar — with the session's open, high,
  low and volume — into the intraday series. The two series then ordered
  differently under different walk orders, and one engine put a different
  row at position 97. Intervals are now labelled per SERIES, by the median
  gap between bars.
- **A page carrying two quotes for one instrument picked whichever was
  walked first.** The Shell capture holds two records taken 13 seconds
  apart; the freshest is now chosen, so two parses of one page agree.
- **`--mode markets` row order depended on which blobs a document held.**
  The indices strip is published twice, so the same 46 rows came out
  sectors-first or indices-first and every row's `position` changed. Rows
  are now sorted by (strip, symbol), which is OUR order and is documented as
  such.

### Notes

- **`--window` does not deepen the chart** — every value from `5D` to `MAX`
  returns the same daily series, because the deeper history is fetched by an
  endpoint this repo does not implement.
- **`financials` exposes 7 of 106 slots.** The payload labels none of them.
  Each of the seven was established by matching the page's own rendered
  figure against every slot across four periods on four instruments in four
  currencies — and the method paid for itself immediately: EPS matched two
  slots on Alphabet and only one on BMW, so a one-instrument mapping had an
  even chance of reading the wrong column. The other 99 slots are left
  alone.
- **EPS is null where Google publishes none** — 0 of 88 periods on
  `7203:TYO` against 90 of 90 on `GOOGL:NASDAQ` — and is deliberately NOT
  filled from the adjacent basic-EPS slot, which is a different measure.
- **The analyst rating slots are not in the order they look to be.** The
  payload holds `[29, "StrongBuy", 25, 0, 4]` for an instrument the page
  renders as "Buy 25 | Hold 4 | Sell 0", so the third count is SELL and the
  fourth is HOLD. Because that is surprising, it is checked at runtime: the
  counts must sum to the stated total and must not contradict the verdict,
  and the three are nulled rather than emitted when they do.


## [0.1.0] — 2026-09-22

First release. Three modes, three engines, all run live against the real site
before publication.

### Added

- **`--mode quote`** — one instrument per `--symbols` entry, with the
  session's open, high, low, volume, market cap, industry and the
  extended-hours quote where the venue publishes one. Equities, indices,
  futures, currency pairs and crypto pairs all parse; each was captured and
  is pinned by a fixture.
- **`--mode markets`** — every strip the market page publishes. Measured
  46 rows on `gl=US`: 20 broad indices, 11 sector indices, 5 currency pairs,
  5 crypto pairs and 5 futures. These middle groups are what Google's
  retired `/finance/markets/{currencies,cryptocurrencies}` URLs used to
  serve.
- **`--mode movers`** — the market page's gainers, losers and most-active.
  A PREVIEW rather than a ranking: Google no longer publishes a full top-N
  at any URL, and the README says so rather than letting a reader infer a
  truncation bug.
- **`--market CC`** — the market to read, sent as Google's own `gl`. This is
  what makes a named market reachable without a proxy: from one Helsinki
  address, `gl=US` returned NASDAQ movers and CBOE sector indices and
  `gl=DE` returned ETR and STOXX.
- **`--symbols`** — the unit of work on this site. `--concurrency` splits
  these rather than pages.
- **`--category`** — keep one strip (`index`, `sector`, `currency`,
  `crypto`, `futures`) or one movers list.
- A shape-anchored payload parser, `page_flow.STATE_POLICY` covering
  `content` / `not_found` / `unsupported_client` / `empty` / `throttled` /
  `blocked`, and 268 offline checks.
- An **ungated daily canary**: the listing modes need no credential, so it
  runs a real scrape from a bare GitHub runner and is expected green. If
  Google ever puts this behind a key, the badge goes red the next morning
  and the README's central claim is retested without anyone remembering to.

### Notes for anyone porting this to another site

Four things here were measured rather than inherited, and each one was a bug
first:

- **`--pages` above 1 is refused, with the reason.** Google Finance does not
  paginate: a quote page is one instrument, and all seven
  `/finance/markets/*` URLs redirect to the market page, which carries its
  lists in one payload. Honouring `--pages 3` would fetch one page three
  times and report a complete three-page run.
- **The parser never anchors on `ds:N`.** The payload's key numbering shifts
  when a query parameter changes the page — `?window=5D` gives 23 keys
  instead of 22 and moves every key up by one.
- **A `Page Not Found` page parses to 35 instrument records** of page
  chrome, so the classifier reads the site's own sentence before it counts
  any rows. It is reported as `not_found` — not as a block, and not as an
  empty market.
- **The gate here is the CLIENT, not the address.** curl with its own
  User-Agent is redirected to an "unsupported browser" page with no data on
  it; curl with a Chrome User-Agent gets the real page from the same
  address. `unsupported_client` is therefore its own state and never counts
  as blocked.

[Unreleased]: https://github.com/2scraper/google-finance-scraper/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/2scraper/google-finance-scraper/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/2scraper/google-finance-scraper/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/2scraper/google-finance-scraper/releases/tag/v0.1.0
