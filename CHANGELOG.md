# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. A PATCH release means *fixes* — it does not promise that
every flag and default is frozen, and where a default changes in one, the
release notes lead with it.

## [Unreleased]

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

[Unreleased]: https://github.com/2scraper/google-finance-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/google-finance-scraper/releases/tag/v0.1.0
