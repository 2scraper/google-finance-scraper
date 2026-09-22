# Troubleshooting

Symptoms first. Every number here was measured on **2026-09-22** from a
datacentre address in Helsinki; re-measure before trusting one.

---

## "Not Found" / exit 4 on a symbol I know exists

**Check the symbol's form first — this is the commonest mistake on this
site, and it does not look like a mistake.**

| kind | form | example |
|---|---|---|
| equity, index, futures | `TICKER:EXCHANGE` | `GOOGL:NASDAQ`, `.INX:INDEXSP`, `GCW00:COMEX` |
| currency pair, crypto | `BASE-QUOTE` | `EUR-USD`, `BTC-USD` |

`EURUSD:CURRENCY` is the trap. It does not 404 — it returns **HTTP 200**
with Google's own *Page Not Found*, and the string `EURUSD` still appears
**17 times** in those bytes, so it reads like a served page to anything
doing a quick grep. The scraper reports `not_found` with the symbol named.

`not_found` is an ANSWER, not a failure: the run does not retry it, does not
rotate an exit, and does not count it as blocked. If you expected a block
and got this, Google simply has no such instrument.

Delisted instruments behave the same way — `SBER:MCX` is `not_found` today.

---

## Zero rows, and the dump says "unsupported"

The sidecar will say `unsupported_client`. **This is not a block.** No
proxy, exit country or captcha solve will change it.

Google Finance redirects clients whose User-Agent does not look like a
browser to `/finance/beta/unsupported` — a 657 KB page with no data on it.
Measured, same address, one fetch each:

| client | result |
|---|---|
| curl, curl's own User-Agent | `/finance/beta/unsupported`, no data |
| curl, a Chrome User-Agent | the real page, 1.31 MB |
| headless Chromium | the real page |

Every engine here sets a browser User-Agent, so seeing this means something
overrode it. Check, in this order: a `--fingerprint` run whose identity is
incomplete, a custom UA stacked on `--cdp-endpoint` (the remote browser
already has one — do not add a second), and anything in your own wrapper
that sets headers.

`Sec-Fetch-Dest/Mode/Site` made no difference in testing, in either
direction; it is the User-Agent.

---

## Exit 3 (blocked)

Genuinely surprising here: **11 fetches from a datacentre address met no
challenge at all**, and every candidate challenge marker scored zero across
all of them.

If you are seeing it, it is Google's `/sorry/index` interstitial, which is
about the ADDRESS rather than the client — the opposite axis from the
section above. A residential `--proxy` is the next thing to try. A 2Captcha
Scraping Browser endpoint brings its own exit *and* its own identity, so use
one or the other and never both.

Worth reporting as an issue if it happens on `--mode markets` or
`--mode movers`: those need no credential of any kind, and a block there is
a site change rather than a you-problem.

---

## "--pages 3" is refused

On purpose. **Google Finance does not paginate.** A quote page is one
instrument, and all seven `/finance/markets/*` URLs — `gainers`, `losers`,
`most-active`, `currencies`, `cryptocurrencies`, `climate-leaders`,
`indexes` — now redirect to the market page, which carries its lists in one
payload.

Honouring `--pages 3` would fetch the same page three times, find no new
symbol, and report a *complete three-page run*. Use `--symbols` to read more
instruments; `--concurrency` splits those.

---

## `movers` returns only 2-4 rows per list

That is what Google publishes. The market page carries a **preview** of each
list, not a ranking — measured 1 to 4 rows per list across six markets — and
the standalone pages that used to serve a full top-N are gone.

There is no flag for this and no paid product that restores it. If you need
breadth rather than the top few, `--mode markets` is the mode that still
returns a full set: 46 rows across five strips.

---

## The movers/sector lists are for the wrong country

Pass `--market`. With no `--market`, Google picks the market from your exit
IP — so a run from a Finnish address returns Helsinki movers, which is
correct behaviour and looks like a bug.

`--market US` from that same address returned NASDAQ movers and CBOE sector
indices; `--market DE` returned ETR and STOXX. **No proxy is involved** —
it is Google's own `gl` parameter.

The sidecar records which market a run read, and `diff_runs.py` refuses to
compare two runs that read different ones: their lists do not overlap, so
every row would report as both added and removed.

---

## `diff_runs.py` refuses to compare my two runs

It refuses for four reasons, and each is named in the message:

* **different markets** — see above;
* **different modes** — a quote row and a list row carry different fields;
* **either run not `complete`** — a run cut short is missing rows that would
  otherwise read as delisted;
* a mode with no stable identity per row.

`--force` overrides all of them. Read what it said first.

---

## Every column is there but `volume` / `market_cap` is null

Expected on indices, currency pairs, crypto and futures. Google writes `0`
in those slots where it means *"there is none"*, and writing a zero through
would drag any average you compute — so both are nulled at the sentinel and
the suite pins that no row carries one.

An index DOES have a real volume (the S&P 500 read 3.28 billion when
measured), so a null there is worth an issue.

---

## The run takes 20 seconds a page

You are on an older build whose readiness selector never matched. The page's
anchors are **relative** (`./quote/...`), so an absolute selector matched 0
elements on a fully painted page and every fetch waited out its whole
timeout before parsing rows it already had.

Fixed in 0.1.0. A healthy run is about 1.5 seconds per instrument — four
symbols took 8.3 seconds end to end, browser launch included.

---

## `smoke_test.py` fails on a hex string in my virtualenv

Your venv is inside the repo and the credential scan walked into it. The
scan recognises a virtualenv by its **`pyvenv.cfg`**, not by its name, so
this should not happen — if it does, the directory is missing that file.
Report it; do not add an exemption for your directory's name, because the
name is exactly what churns.

## `ci_checks.py` says "0 files scanned"

The scan asks `git ls-files` for its list, so this means the directory is
not a git repository (or the index is unreadable) — **not** that there is
nothing to scan. It is a failure for that reason. Run `git init && git add
-A` first.
