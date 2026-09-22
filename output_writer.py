"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Two modes, one row shape
------------------------
    --mode listing   a keyword search or a genre listing -> Product
    --mode product   one item.google.com/finance/{shop}/{code}/ page -> Product,
                     with the detail-only fields populated

Both modes yield the SAME class, and on this site that is load-bearing
rather than tidy. Google Finance states a product's identity as
`{shopCode}:{manageNumber}` — the detail page publishes it as
`<meta itemprop="sku">` and it is recoverable from the URL on both routes —
so a listing row and a product row for the same product carry a
byte-identical `sku` and a consumer can JOIN the two files on it. Verified
live: a product run of `sawaicoffee-tea:solandluna` and a genre listing run
that happened to contain it agreed on the id exactly.

What that does NOT buy is a cross-mode DIFF, and `diff_runs.py` still
refuses one (`--force` overrides). The ids line up; the row SETS do not. A
90-row listing run against a 1-row product run would report 89 products
removed, and every line of it would be an artefact of the two runs covering
different things. Same reasoning as the family's refusal to diff a partial
run: the join key being right is necessary and not sufficient.

There is deliberately no `--mode shop`. A merchant's storefront at
`www.google.com/finance/{shopCode}/` looks like it should be a third mode and is
not: its `__INITIAL_STATE__.state.data` is EMPTY and it carries no
`the payload` payload at all (measured 2026-09-21), so the mode would need
a second parser written against markup nobody has captured. A mode that
ships untested is worse than a mode that is absent — `product_parser`
refuses that URL with that reason instead.

`Product` keeps the family's first eighteen columns in the family's order,
with Google Finance's own ones appended after `position`, so a consumer written
against another repo in this family still reads the prefix unchanged.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type, Union


# The hostname a row came from. Google Finance is ONE marketplace reached
# through several hostnames — `search.google.com/finance` for the result grid,
# `www.google.com/finance` for a genre landing page, `item.google.com/finance` for an
# individual product — and they serve one catalogue in one currency. So this
# column is `google.com/finance` on every row of every run rather than the
# hostname of the moment; which route a row came from is recorded by
# `price_source`, and the merchant behind it by `shop_code`.
#
# It is kept in this position because the family's schema has it here and
# consumers read the columns by name across repos.
SOURCE_DEFAULT = "google.com/finance"


@dataclass
class Quote:
    """One row per INSTRUMENT quote.

    The first five fields are the family prefix, byte-identical and in the
    same order as every other repo in this family, so one consumer reads a
    google-finance run and a polymarket run with the same code. `price` and
    `currency` follow because this site genuinely publishes both; everything
    after them is Google Finance's own.

    The prefix stops at `title` deliberately. The commerce columns the older
    repos carry — brand, original_price, discount_pct, rating, review_count,
    in_stock, image_url — have no referent on a quote, and CLAUDE.md §9 says
    a column that is null on every row of every run should not exist. The
    non-commerce siblings settled this before this repo existed: wellfound
    (jobs) and quora (answers) both keep exactly these five and then go their
    own way, and polymarket keeps these five plus price and currency, which
    is the shape adopted here.

    Three things about the numbers, each measured rather than assumed:

      * `market_cap` and `volume` are ZERO rather than null on instruments
        that have no such figure — 0 on the index, currency and crypto
        captures of 2026-09-22. Written through, that zero drags any average
        a consumer computes, so both are nulled at the sentinel and a check
        pins that no row carries a zero in either. CLAUDE.md §21's "zero is
        not a rating", on a fourth site.
      * `prev_close` is NOT mapped onto the family's `original_price`. It is
        not a was-price: a quote below its previous close is not discounted,
        and `discount_pct` computed from the two would print a negative
        percentage for an ordinary down day.
      * `after_hours_*` is populated only when the venue publishes an
        extended-hours quote, which on the 2026-09-22 captures means US
        equities and nothing else. A null there is "this venue has no
        extended session", not a failed read.
    """
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # The instrument's own absolute URL on www.google.com/finance, in the
    # `/finance/beta/quote/{symbol}` form the site redirects every older
    # spelling to. Never rebuilt from a browsed host.
    url: str = ""
    # `TICKER:EXCHANGE` for anything traded on a venue (`GOOGL:NASDAQ`,
    # `.INX:INDEXSP`, `GCW00:COMEX`) and `BASE-QUOTE` for currency pairs and
    # crypto (`EUR-USD`, `BTC-USD`). This is Google's own canonical symbol,
    # read out of the payload rather than rebuilt from the URL: the page
    # states it at a fixed offset in every instrument record, and it is the
    # one id that addresses the URL and joins across all three modes.
    #
    # Deliberately NOT recovered from the URL. A URL's symbol is whatever the
    # caller typed, and the caller can type one the site does not have:
    # `EURUSD:CURRENCY` returns HTTP 200 with the site's own Page Not Found,
    # and the string `EURUSD` still appears 17 times in those bytes. A sku
    # taken from the URL would therefore be populated on a page holding no
    # such instrument.
    sku: Optional[str] = None
    # The instrument's display name as Google states it — "Alphabet Inc Class
    # A", "Bayerische Motoren Werke AG", "S&P 500".
    title: Optional[str] = None
    # The last traded price in `currency`. Null only where the payload states
    # none; never defaulted to 0, which is a real price.
    price: Optional[float] = None
    # ISO 4217, as the payload states it. Null on indices, which Google
    # publishes without one — an index level is not money. Never defaulted to
    # "USD" (CLAUDE.md §4's currency ladder: absent is null, not a guess).
    currency: Optional[str] = None

    # --- Google Finance's own ------------------------------------------
    # The two halves of `sku`, split out so a consumer can group by venue
    # without parsing the id. `exchange` is null on a currency or crypto
    # pair, which names no venue.
    ticker: Optional[str] = None
    exchange: Optional[str] = None
    # "stock" | "index" | "currency" | "crypto" | "futures", derived from the
    # payload's own type flag and symbol form rather than from the URL.
    instrument_type: Optional[str] = None
    # Session figures, all in `currency`.
    prev_close: Optional[float] = None
    open: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    change: Optional[float] = None
    change_pct: Optional[float] = None
    # Shares (or contracts) traded. Nulled at the 0 sentinel — see the class
    # docstring.
    volume: Optional[int] = None
    # Nulled at the 0 sentinel likewise.
    market_cap: Optional[float] = None
    # Google's own industry label ("Interactive media", "Car"). Null on
    # everything that is not a company.
    industry: Optional[str] = None
    # ISO 3166-1 alpha-2 of the listing venue's country, as the payload
    # states it.
    country: Optional[str] = None
    # IANA zone and its offset in seconds, both as the payload states them.
    # Kept because a quote without its venue's clock cannot be aligned
    # against another venue's.
    timezone: Optional[str] = None
    utc_offset: Optional[int] = None
    # The moment Google says this quote is as of, ISO 8601 UTC — NOT the
    # moment this run fetched it, which is `scraped_at`. A stale quote is
    # visible only in the difference between the two.
    quoted_at: Optional[str] = None
    # Extended-hours quote where the venue publishes one. See the docstring.
    after_hours_price: Optional[float] = None
    after_hours_change: Optional[float] = None
    after_hours_change_pct: Optional[float] = None
    # Which of the root page's lists this row came from in `--mode markets`
    # and `--mode movers` — "index", "sector", "gainers", "losers",
    # "most_active". Null in `--mode quote`, which asks for one instrument by
    # name rather than reading a list.
    #
    # Load-bearing for dedupe: one symbol can be in two lists at once, so
    # rows are unique by (sku, listing) rather than by sku alone in the two
    # list modes.
    listing: Optional[str] = None
    # The market whose lists were read — the `gl` the run asked for, upper
    # case ("US", "DE"), or null when the run let the exit IP decide. The
    # root page's lists are geo-selected, so two runs of `--mode movers` from
    # different markets are different samples and not a change in the data;
    # `diff_runs.py` refuses to compare across it for that reason.
    market: Optional[str] = None
    # Google's own entity id for the instrument (`/m/07zln7n`, `/g/1dvbt7jl`).
    # Stable across markets and languages where the display name is not, so
    # it is what a cross-market join keys on.
    entity_id: Optional[str] = None
    # Which payload record the row was built from — "af_quote" for the rich
    # per-instrument record a quote page carries, "af_list" for the compact
    # record a list row carries. The two carry different columns (a list row
    # has no open/high/low), so a consumer can tell a measured absence from a
    # missing read.
    price_source: Optional[str] = None
    # 1-based, and in this repo they index INSTRUMENTS rather than pages:
    # none of the three modes paginates (see product_parser.paginates_by_url),
    # so `page` is the index of the symbol within a `--symbols` run and
    # `position` is the row's place within the list it came from.
    page: Optional[int] = None
    position: Optional[int] = None


# Keep the family name pointing at the row class. `tests.yml` carries an
# inline `from output_writer import Product`, and a sibling's first push to a
# public repo went red on exactly that after renaming its row class with the
# alias left out.
Product = Quote


# One kind of thing, one dataclass. All three modes read the same object —
# an instrument quote — differing only in how many of them a page holds and
# how richly each is described, so there is no second row class to keep in
# step.
ROW_CLASS_BY_MODE = {"quote": Quote, "markets": Quote, "movers": Quote}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku`
# alone and to hand to diff_runs.py.
#
# Only `quote` qualifies, and that is measured rather than stylistic. A
# stock can be a gainer and among the most active in the same session, so
# one symbol lands in two of the root page's lists at once: counted on the
# 2026-09-22 captures, 2 symbols on the US market, 1 on the exit IP's own
# and 0 on the German one. Deduping those on `sku` would silently drop
# whichever list was parsed second and make the surviving row's `listing`
# column depend on parse order. The list modes dedupe on (sku, listing)
# instead — see DEDUPE_KEY_BY_MODE.
#
# `markets` is in the same boat for a different reason: its rows are unique
# by symbol WITHIN a run, but a symbol's strip is a property of the run's
# market, so the compound key is what keeps two markets' rows distinct if
# anyone concatenates them.
UNIQUE_BY_SKU_MODES = ("quote",)

# What makes a row unique, per mode. A mode absent from here dedupes on
# `sku`.
DEDUPE_KEY_BY_MODE = {"markets": ("sku", "listing"), "movers": ("sku", "listing")}


def dedupe_by_key(rows: Sequence[Any], seen: Set[Any],
                  key: Union[str, Sequence[str]] = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across
    fetches — a repeated url then re-parses a page without duplicating its
    rows into the final output.

    `key` may be a field name or a TUPLE of them, and the tuple case is not
    decoration. In this repo's list modes a row is unique by (sku, listing)
    rather than by sku alone: a stock can be a gainer and among the most
    active in the same session, so it appears in two of the root page's
    lists — measured 2 symbols on the 2026-09-22 US capture, 1 on the exit
    IP's own market and 0 on the German one. Deduping those on `sku` would
    drop whichever list was parsed second and make the surviving row's
    `listing` depend on parse order.

    A row with no key value is always kept: there is nothing to check a
    duplicate against, and dropping it would be a silent data loss rather
    than a duplicate removal. For a compound key that means a row is kept
    when EVERY part is None — one populated part is enough to compare on,
    because two rows agreeing on `sku` and both lacking `listing` really are
    the same row twice.
    """
    fields = (key,) if isinstance(key, str) else tuple(key)
    fresh = []
    for r in rows:
        values = tuple(getattr(r, f, None) for f in fields)
        val = values[0] if len(fields) == 1 else values
        if all(v is None for v in values) or val not in seen:
            if not all(v is None for v in values):
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    # `tags` and each entry of `variants` are mappings, and a Python repr
    # of one is neither readable in a spreadsheet nor parseable by anything
    # but Python.
    # Compact JSON is both, and round-trips through json.loads.
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the three ways to get a
# real page with no products on it: a `/p/<slug>` discovery hub, which
# answers 200 with banners and carousels and no grid; a search whose query
# matches nothing ("Oops, produk nggak ditemukan"); and one page past the
# end of a category listing. All three are EXIT_NO_PRODUCTS — the request
# was served exactly as asked and simply has no products on it. Reporting
# any of them as blocked would send a user hunting for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here is unusually literal: this site refuses a
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "quote", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: the same output prefix can hold a listing run or a product run,
    and those populate different columns — `sold` is a FLOOR on a listing
    row and exact on a product row, so diffing one against the other would
    report every row as changed. diff_runs.py refuses a pair whose modes or
    sources differ. `source` is `google.com/finance` on every row of every run
    here, since the site has one storefront and one currency; it is kept
    because consumers read these columns by name across the family.

    `extra` carries facts about the run that are not about any single row.
    `--mode shop` uses it for the SELLER's own name, location, rating and
    review count: a run covers exactly one shop, so those belong to the run
    rather than repeated down a column, and the shop's review count (16679
    on the captured seller) is a different number from its listings' own
    (827 on one of them) — putting them in one column would make the schema
    lie.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue. On this site that ordering is not a preference, it is the only
# thing that works: the site publishes NO `link[rel=next]` and no numbered
# anchors anywhere, a CATEGORY listing is addressable by `?page=N`, and a
# SEARCH is not addressable at all — `?page=2` there returns an empty result
# set rather than page 2. So "no new products" is the one termination
# condition available on a search. See page_flow.pagination_is_addressable.
#
# "single_page_mode" is complete by construction: --mode product reads one
# page because one page is all there is.
# `end_of_listing` is a COMPLETE result and leaving it out of this tuple is a
# bug worth naming, because it produced exit 6 for a correct run. On this
# site a listing does not end with an error or an empty page: Google Finance answers
# a request past the last page by serving page 1 again under HTTP 200, and
# the engine detects that from the offset the server states rather than from
# its own request. Having found the real end of the results, the run has
# everything the site will give it.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode", "end_of_listing")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "quote", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
