# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. A PATCH release means *fixes* — it does not promise that
every flag and default is frozen, and where a default changes in one, the
release notes lead with it.

## [Unreleased]

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

[Unreleased]: https://github.com/2scraper/google-finance-scraper/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/2scraper/google-finance-scraper/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/2scraper/google-finance-scraper/releases/tag/v0.1.0
