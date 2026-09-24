# google-finance-scraper

[![tests](https://github.com/2scraper/google-finance-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/google-finance-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/google-finance-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/google-finance-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)](#engines)
[![runs without an account](https://img.shields.io/badge/runs-without%20an%20account-success)](#what-you-actually-need)

Scrapes **Google Finance** — instrument quotes, quarterly financials, analyst
ratings and price targets, OHLCV bars, the market index and sector strips,
currency, crypto and futures, the day's gainers and losers, and the earnings
calendar — with Playwright, Selenium, Puppeteer, or the 2Captcha **Scraping
Browser API** over CDP. JSON and CSV out.

Seven modes, all reading the page's own server-rendered payload. **No key, no
proxy, no account.**

---

## What you actually need

**No key, no account, no browser infrastructure — and no proxy, from a
region Google serves Finance in.**

That last clause is not hedging. A third-party audit hit it on 2026-09-24,
and it was then measured directly through pinned residential exits — ten
countries, one request each:

| exit | result |
|---|---|
| **RU** | **HTTP 403, refused** (4 of 4: three quote fetches and the market page) |
| US, DE, FR, HU, PL, CZ, AT | HTTP 200, served |

What a refused region gets:

```
403. That's an error.
Google Finance is currently not supported in your region.
```

**`--market` does not get you past that**, and the distinction matters
because the flag looks like it should. Verified from the refused exit:
`?gl=US&hl=en` answered 403 with the same message, on both the quote route
and the market page. `gl` selects WHICH market's data you are served; the
regional gate decides whether you are served at all, and it is checked
first.

From a refused region you need an exit in a supported one — a `--proxy`, or
the `country-` segment of a Scraping Browser endpoint. From a supported
region you need nothing.

The scraper now recognises that page as `region_unavailable` and reports
exit 3 with advice naming the country axis, rather than the exit 4 it used
to report, which means "the page loaded and the market is empty".

Everything below was measured from one datacentre address (Hetzner,
Helsinki, AS24940) on **2026-09-22**, with no credential of any kind:

| command | result |
|---|---|
| `--mode markets --market US` | **46 rows**, every one priced |
| `--mode movers --market US` | **11 rows** across three lists |
| `--mode quote --symbols GOOGL:NASDAQ,BMW:ETR,EUR-USD,BTC-USD` | **4 rows in 8.3 s** |
| `--mode financials --symbols GOOGL:NASDAQ` | **90 quarters**, back to 2004 |
| `--mode analysts --symbols GOOGL:NASDAQ` | **44 analyst actions** + the consensus |
| `--mode chart --symbols GOOGL:NASDAQ` | **99 bars** — 79 intraday, 20 daily |
| `--mode earnings --market US` | **5 upcoming announcements** |

Within a supported region, the site is unusually open, and the reason is
worth stating plainly because it changes what the paid products are for:
**which market you read is a query parameter, not a property of your exit
address.** `--market US` from a Finnish address returns NASDAQ movers and
CBOE sector indices; `--market DE` returns ETR and STOXX; with no `--market`
at all you get whatever market your exit IP sits in. Measured, one fetch
each, same address.

So one usual reason to buy an exit — *"I need to see the American market"* —
does not apply. The other one does: if your own region is refused, an exit
is the only way in. What the 2Captcha products buy:

* **proxies** — access from a refused region, and volume. Spreading a large
  run across exits is what keeps any one address from being scored.
* the **Scraping Browser API** — no local browser to install or keep
  patched, and a persistent profile.
* **captcha solving** — see [Captchas](#captchas). None was met in testing;
  the detection is wired for the day one is.
* **fingerprints** — a consistent device identity.

---

## Install

One engine, in its own virtualenv. The three engines' pins are mutually
unsatisfiable (`pyee <12` vs `>=13`, `urllib3 <2.0` vs `>=2.6`), so install
exactly one per environment:

```bash
git clone https://github.com/2scraper/google-finance-scraper
cd google-finance-scraper
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
./.venv/bin/playwright install chromium
./.venv/bin/python smoke_test.py          # offline checks, no network
```

## Use

```bash
# one instrument, or several — the unit of work on this site is the SYMBOL
python3 playwright_scraper.py --mode quote \
    --symbols GOOGL:NASDAQ,BMW:ETR,EUR-USD --out quotes

# every strip the market page publishes: indices, sectors, FX, crypto, futures
python3 playwright_scraper.py --mode markets --market US --out markets

# just one strip
python3 playwright_scraper.py --mode markets --market DE --category currency

# the day's movers
python3 playwright_scraper.py --mode movers --market GB --out movers

# quarterly financials, back to 2004 on a large cap
python3 playwright_scraper.py --mode financials --symbols GOOGL:NASDAQ

# the consensus, the target range, and every published analyst action
python3 playwright_scraper.py --mode analysts --symbols GOOGL:NASDAQ

# OHLCV — the latest session at 5-minute resolution AND a month of daily
# bars, in one run with no extra fetch
python3 playwright_scraper.py --mode chart --symbols GOOGL:NASDAQ,BMW:ETR

# who reports this week
python3 playwright_scraper.py --mode earnings --market US
```

### Symbol spelling, and the one trap

| kind | form | example |
|---|---|---|
| equity, index, futures | `TICKER:EXCHANGE` | `GOOGL:NASDAQ`, `.INX:INDEXSP`, `GCW00:COMEX` |
| currency pair, crypto | `BASE-QUOTE` | `EUR-USD`, `BTC-USD` |

**The colon form of a pair is the trap.** `EURUSD:CURRENCY` does not error —
it returns HTTP 200 with Google's own *Page Not Found*, and the string
`EURUSD` still appears 17 times in those bytes. The scraper reports
`not_found` with the symbol named, rather than an empty result.

---

## Modes

**Instrument modes** — one `--symbols` entry each, one page fetch each:

| mode | reads | rows (GOOGL:NASDAQ, 2026-09-22) |
|---|---|---|
| `quote` | the current quote | **1** — open/high/low, volume, market cap, industry, extended hours |
| `financials` | one row per reporting period | **90** quarters back to 2004 — revenue, net income, operating expense, EBITDA, EPS, net margin, effective tax rate, and what the street estimated |
| `analysts` | the consensus and every published action | **44** — verdict, buy/hold/sell split, 12-month target range, plus each firm, date, rating and price target |
| `chart` | every OHLCV bar the page carries | **99** — 79 intraday (5-minute) and 20 daily |

**Market modes** — one page fetch, geo-selected by `--market`:

| mode | reads | rows (gl=US, 2026-09-22) |
|---|---|---|
| `markets` | every strip on the market page | **46** — 20 indices, 11 sectors, 5 FX, 5 crypto, 5 futures |
| `movers` | that page's gainers / losers / most-active | **11** |
| `earnings` | its upcoming announcements calendar | **5**, with revenue and EPS estimates |

Everything above comes out of the **same first response**. The site's
Overview / Analysis / Earnings / Financials tabs are rendered client-side
from data that already arrived, so `--mode financials` costs exactly one
page fetch — the same one `--mode quote` makes.

### Three limitations worth reading before you plan around them

**`movers` returns a PREVIEW, not a ranking.** Google retired the standalone
`/finance/markets/gainers`, `/losers`, `/most-active`, `/currencies`,
`/cryptocurrencies`, `/climate-leaders` and `/indexes` pages — all seven now
redirect to the market page, checked 2026-09-22 — and what that page
publishes is the top 1 to 4 of each list, measured across six markets. A
full top-50 gainers ranking is not available from this site, at any price,
to anyone. This repo reads what Google publishes and says so rather than
letting you infer a truncation bug.

The same change is why `markets` is the valuable mode here: the currency,
crypto and futures strips it returns **are** what those retired URLs used to
serve, and they are still published in full.

**`--window` does not deepen the chart.** Every value from `5D` to `MAX`
returns the same ~20 daily bars, because the deeper history is fetched
client-side by an endpoint this repo does not implement. What you get for
free is one full session at five-minute resolution plus about a month of
daily bars.

**`financials` exposes seven figures, not a full statement.** Google
publishes 106 unlabelled numbers per quarter; seven of them were identified
by matching the page's own rendered rows against every slot across four
periods on four instruments in four currencies. The other 99 are left alone,
because a mislabelled financial figure is worse than a missing one. EPS is
null on instruments where Google publishes none — measured 0 of 88 periods
on `7203:TYO`, against 90 of 90 on `GOOGL:NASDAQ` — and is deliberately not
filled in from the adjacent basic-EPS slot, which is a different measure.

---

## Output

Five row classes, one per kind of thing. `quote`, `markets` and `movers`
share `Quote` (32 columns); `financials`, `analysts`, `earnings` and `chart`
each have their own. All five open with the same five columns — `source`,
`scraped_at`, `url`, `sku`, `title` — so `sku` joins every mode to every
other, and `diff_runs.py` refuses to compare two modes.

The quote row, 32 columns, same JSON and CSV field order. See
[`sample_output.json`](sample_output.json) — cut from a real run, not
written by hand.

```json
{
  "source": "google.com/finance",
  "url": "https://www.google.com/finance/beta/quote/GOOGL:NASDAQ",
  "sku": "GOOGL:NASDAQ",
  "title": "Alphabet Inc Class A",
  "price": 354.97, "currency": "USD",
  "ticker": "GOOGL", "exchange": "NASDAQ", "instrument_type": "stock",
  "prev_close": 349.54, "open": 350.64, "day_high": 357.61, "day_low": 349.1,
  "volume": 152020, "market_cap": 4312487948430.176,
  "industry": "Interactive media", "country": "US",
  "timezone": "America/New_York",
  "after_hours_price": 356.95,
  "listing": null, "market": null, "price_source": "af_quote"
}
```

Notes that will save you a wrong conclusion:

* **`sku` is Google's own canonical symbol**, read out of the payload rather
  than rebuilt from the URL — because a URL's symbol is whatever you typed,
  and you can type one the site does not have.
* **`market_cap` and `volume` are `null`, never `0`.** Google writes `0`
  where it means "there is none" — on every index, currency, crypto and
  futures row measured. Written through, that zero drags any average you
  compute, so both are nulled at the sentinel.
* **`prev_close` is not an `original_price`.** A quote below its previous
  close is not discounted; there is deliberately no `discount_pct` column
  here.
* **`quoted_at` is Google's as-of, `scraped_at` is ours.** A stale quote is
  visible only in the difference.
* **`currency` is null on an index.** An index level is not money, and
  nothing here defaults to `"USD"`.
* **`market` is load-bearing.** The market page's lists are geo-selected, so
  a `gl=US` run and a `gl=DE` run are two SAMPLES, not a before and an
  after. `diff_runs.py` refuses to compare across it.

### Exit codes

`0` ok · `1` crash · `2` bad usage · `3` blocked · `4` zero rows from a page
that *was* fetched · `5` the content was never obtained · `6` partial.

`4` and `5` are deliberately different: an empty market and a dead proxy must
not be one value to a pipeline.

---

## How it reads the page

Google Finance renders its whole payload into `AF_initDataCallback({key:
'ds:N', data:[...]})` blobs — plain nested JSON, fully server-rendered, in
the first response. There is **no JSON-LD at all**: measured **zero**
`application/ld+json` blocks across 11 captures.

**The parser never anchors on `ds:N`, and that is measured.** The key
numbering is stable across repeated fetches of the same URL and **shifts the
moment a query parameter changes the page**: `?window=5D` produces 23 keys
instead of 22 and moves every key up by one. Anchoring on a key number is
the same mistake as anchoring on a build-hash CSS class, so everything is
found by the SHAPE of the record instead. The suite pins this by renumbering
every key in a real fixture and asserting no row changes.

Because the payload is in the first response, there is no scroll loop and no
hydration wait — an HTTP client with a browser User-Agent and headless
Chromium parse to the same rows.

### There is no pagination, anywhere

A quote page is one instrument; the market page carries its lists in full.
`--pages` above 1 is **refused with the reason** rather than honoured, which
would fetch one page three times and report a complete three-page run.
`--concurrency` splits **symbols**, not pages.

---

## Blocks, and what actually gets refused

The thing Google refuses here is usually **the client, not the address** —
and the direction is the opposite of what this family's other sites taught.
Same address, same URL, one fetch each, 2026-09-22:

| client | result |
|---|---|
| curl, curl's own User-Agent | redirected to `/finance/beta/unsupported`, 657 KB, **no data** |
| curl, a Chrome User-Agent | the real page, **1.31 MB** |
| headless Chromium | the real page |

`Sec-Fetch-Dest/Mode/Site` made no difference either way. So on this site a
browser User-Agent is *required*, where a sibling site refuses curl
precisely **for** wearing one. Every engine here sets one, so if you ever
see `unsupported_client` in the sidecar, something overrode it — that state
is reported as its own thing and **never as a block**, because no proxy,
exit country or captcha solve would change it.

A real block is Google's `/sorry/index` interstitial, which is about the
address. None was met in 11 fetches from a datacentre address.

## Captchas

**No captcha was rendered in testing.** Every candidate marker scored
**zero** across all 11 captures, served and refused alike.

That is a measurement from one address on one day, and explicitly **not** a
claim that Google does not challenge scrapers. It serves `/sorry/index` with
a reCAPTCHA to addresses it scores badly; this repo recognises that page, and
2Captcha solves that task type (`RecaptchaV2TaskProxyless` /
`RecaptchaV3TaskProxyless`). The solver stays wired and costs nothing while
no challenge appears — `--solve-captcha when-blocked` is the default and
counts instrument links on the spot rather than paying for a page whose rows
are already there.

---

## Engines

All three produce **identical rows**: verified 2026-09-22 on the same URL,
46 rows each, **zero** differing stable columns between Playwright and its
twins.

| engine | notes |
|---|---|
| **Playwright** | primary. The only one with `--concurrency`. |
| **Selenium** | cannot use an authenticated remote CDP endpoint (`debuggerAddress` takes a bare `host:port`), and `--proxy-server` cannot authenticate at all — credentials are stripped with a warning. |
| **Puppeteer** (pyppeteer) | effectively unmaintained upstream; its own README points at Playwright. Adds `--chromium-path`. |

## Configuration

Credentials go in `.env` beside the scripts, **never on a command line** —
anything that can run `ps` reads an argv, and shell history keeps it.

```bash
cp .env.example .env
python3 env_config.py     # prints what was picked up, without printing secrets
```

Precedence, highest first: **explicit flag → exported environment variable →
`.env` → default.** A copied `.env.example` reads as *unset*: every
placeholder still containing `{...}` is treated as absent, so it is never
sent to an API as if it were a key.

---

## Development

```bash
python3 smoke_test.py            # 268 offline checks, no network
python3 .github/ci_checks.py --all
python3 make_fixtures.py         # regenerate fixtures from your own captures
```

Fixtures live in `fixtures_generated.json`, cut from real captures by
`make_fixtures.py`, which **proves** each trimmed fixture parses identically
to the capture it came from — column for column — before writing it.

`captures/` is deliberately not in the repository: eleven captures of this
site are 13 MB.

## Licence

MIT. Not affiliated with, endorsed by, or connected to Google. Respect the
site's terms and the law where you operate; this is a tool, and what you do
with it is yours.
