# google-finance-scraper

[![tests](https://github.com/2scraper/google-finance-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/google-finance-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/google-finance-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/google-finance-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)](#engines)
[![runs without an account](https://img.shields.io/badge/runs-without%20an%20account-success)](#what-you-actually-need)

Scrapes **Google Finance** — instrument quotes, the market index and sector
strips, currency, crypto and futures, and the day's gainers, losers and
most-active — with Playwright, Selenium, Puppeteer, or the 2Captcha
**Scraping Browser API** over CDP. JSON and CSV out, one row schema shared
with the rest of this family.

---

## What you actually need

**Nothing.** No key, no proxy, no account, no browser infrastructure.

That is measured, not a pitch. From one datacentre address (Hetzner,
Helsinki, AS24940) on **2026-09-22**, with no credential of any kind:

| command | result |
|---|---|
| `--mode markets --market US` | **46 rows**, every one priced |
| `--mode movers --market US` | **11 rows** across three lists |
| `--mode quote --symbols GOOGL:NASDAQ,BMW:ETR,EUR-USD,BTC-USD` | **4 rows in 8.3 s** |

The reason this site is unusually open is worth stating plainly, because it
changes what the paid products are for: **on Google Finance the market is a
query parameter, not a property of your exit address.** `--market US` from a
Finnish address returns NASDAQ movers and CBOE sector indices; `--market DE`
returns ETR and STOXX; with no `--market` at all you get whatever market
your exit IP sits in. Measured, one fetch each, same address.

So the usual reason to buy an exit — *"I need to see the American market"* —
does not apply here. What the 2Captcha products still buy:

* **proxies** — volume. Spreading a large run across exits is what keeps any
  one address from being scored.
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

| mode | reads | rows (gl=US, 2026-09-22) |
|---|---|---|
| `quote` | one instrument page per `--symbols` entry | 1 per symbol, with open/high/low, volume, market cap, industry and the extended-hours quote |
| `markets` | every strip on the market page | **46** — 20 indices, 11 sectors, 5 FX, 5 crypto, 5 futures |
| `movers` | that page's gainers / losers / most-active | **11** |

### A limitation worth reading before you plan around it

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

---

## Output

One row per instrument, 32 columns, same JSON and CSV field order. See
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
