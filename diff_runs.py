#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku` —
the identifier the README already tells people to diff on for price
monitoring and assortment tracking, but that nothing in this repo actually
computed.

    python3 diff_runs.py --old watches.2026-09-01.json \\
                          --new watches.2026-09-07.json

Typical use is a scheduled re-run of one of the four scraper engines, kept
under a dated filename, diffed against the previous one:

    python3 playwright_scraper.py --url "$URL" --out "girls_$(date +%F)"
    python3 diff_runs.py --old "girls_$(ls -t girls_*.json | sed -n 2p)" \\
                          --new "girls_$(date +%F).json" --out diff.json

Four buckets, each keyed on sku:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new (delisted, or just
                   off this particular page/category run)
  changed        — sku present in both, with a different price,
                   original_price, discount_pct, currency or in_stock
  source_changed — sku present in both with a different price, but also a
                   different price_source: one run got the DOM-corrected
                   figure and the other the raw JSON-LD one, so the two are
                   not comparable on price. Reported separately because this
                   says something about our own two snapshots, not about the
                   site — and --fail-on-change deliberately ignores it.

A product this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed", which would be wrong on its face.
"""

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

from output_writer import UNIQUE_BY_SKU_MODES, DEDUPE_KEY_BY_MODE

# What a price monitor on Google Finance actually needs to watch, which is more
# than the price.
#
# `points` and `point_rate` are in here and that is the Google Finance-specific
# decision worth explaining: a 10x point campaign on this marketplace is
# effectively a 10% discount that never touches the price column. A monitor
# watching `price` alone would call a product unchanged through the whole of
# a Super Sale. `points` was non-null on 405 of 405 measured rows, so it is
# a column that reliably carries the signal.
#
# `price_max` is here because 95 of 405 rows are a RANGE — an item whose
# variants differ — so a change in the top of the range is a real price
# change that the bottom of it can hide. `subscription_price` likewise: it is
# a different offer on the same product (54 of 405 rows), always below
# `price`, and a shop can move it on its own.
#
# NOT tracked: `rating` and `review_count`, which drift upwards constantly
# and would make every diff noisy, and `genre_rank`, which is the item's
# standing in a listing this run never fetched.
TRACKED_FIELDS = ("price", "original_price", "discount_pct", "currency",
                  "price_max", "subscription_price", "points", "point_rate",
                  "shipping_fee", "free_shipping", "in_stock")

# The subset of TRACKED_FIELDS whose comparability depends on price_source
# matching between the two runs — see diff_products.
#
# All five money columns are in it, and on this site that guard earns its
# keep across MODES rather than across rendering states: a listing row
# (`price_source: "state"`) publishes no was-price at all while a product row
# (`"itemdata"`) publishes one where Google Finance's own verification flag allows
# it. So diffing a listing run against a product run would otherwise report
# a discount appearing on every product in the file, and not one of those
# would be a price change.
# What a diff reports a change in, across all five row classes. A field
# absent from a row class is simply never compared, so one tuple serves all
# of them.
PRICE_FIELDS = ("price", "prev_close", "change", "change_pct",
                "target_mean", "target_low", "target_high",
                "revenue", "net_income", "eps", "close")


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _row_key(row: dict, mode: Optional[str]) -> Optional[tuple]:
    """What identifies a row across two runs, for `mode`.

    `sku` alone only works where a row IS an instrument. It is not: a
    financials run has ninety rows sharing one sku, an analysts run has
    forty-four, and a chart run has ninety-nine. Matching those on sku
    reported "0 added, 0 removed, 0 changed" and "178 rows could not be
    matched" — a diff of nothing, presented as a clean result, which is
    worse than a refusal.

    The key is `output_writer.DEDUPE_KEY_BY_MODE`, which is the same
    definition the writers dedupe on, so a row that survived dedupe is
    exactly a row this can match.
    """
    fields = DEDUPE_KEY_BY_MODE.get(mode or "", "sku")
    if isinstance(fields, str):
        fields = (fields,)
    values = tuple(row.get(f) for f in fields)
    return None if all(v is None for v in values) else values


def _by_sku(products: List[dict],
            mode: Optional[str] = None) -> Tuple[Dict[tuple, dict], int]:
    """Index a run's rows by whatever identifies them in `mode`.

    Kept under its old name: every repo in this family calls it, and the
    thing it does — index a run so two runs can be matched — has not
    changed.
    """
    indexed: Dict[tuple, dict] = {}
    unmatchable = 0
    for p in products:
        key = _row_key(p, mode)
        if key is None:
            unmatchable += 1
            continue
        # A run's own output can already hold a duplicate key — keep the
        # first and count the rest as unmatchable rather than letting one
        # clobber the other silently.
        if key in indexed:
            unmatchable += 1
            continue
        indexed[key] = p
    return indexed, unmatchable


def _within_tolerance(before: dict, after: dict, changes: dict,
                      tolerance_pct: float) -> bool:
    """True if every differing price field moved by less than `tolerance_pct`.

    Inherited from this family rather than earned here, and said plainly
    because the alternative is a comment inventing a reason. A sibling repo
    needs it: that site converts prices for a cross-border visitor and the
    exchange rate ticks between two runs of the same command.

    THIS SITE IS THE OPPOSITE CASE, and the flag is more useful here than
    in any sibling rather than less. A quote MOVES: two runs of `--mode
    markets` four minutes apart, measured 2026-09-22, differed on crypto
    and FX rows by fractions of a percent — BTC 86279.23 -> 86210.33,
    EUR-USD 1.14501632 -> 1.14502 — because the market is open and prices
    are supposed to move. That is not drift to absorb, it is the data; a
    price monitor that wants only material moves is exactly who should set
    this.

    So the flag stays available and DEFAULTS TO ZERO, which makes it inert
    unless someone deliberately asks for it. Set it to something non-zero
    only with a reason you can state; a price monitor that silently swallows
    small moves is worse than one that cries wolf.

    A move is judged on the LARGEST relative change among the price fields,
    so a genuine 0.5% cut is not hidden by a 0.04% tolerance applied
    field-by-field.
    """
    if tolerance_pct <= 0:
        return False
    for field in PRICE_FIELDS:
        if field not in changes:
            continue
        was, now = before.get(field), after.get(field)
        if not isinstance(was, (int, float)) or not isinstance(now, (int, float)):
            return False  # a None appearing or disappearing is a real change
        if was == 0:
            return False
        if abs(now - was) / abs(was) * 100.0 > tolerance_pct:
            return False
    return True


def diff_products(old: List[dict], new: List[dict],
                  price_tolerance_pct: float = 0.0,
                  mode: Optional[str] = None) -> dict:
    # `mode` decides what identifies a row. Defaulting to None keeps the
    # family's signature working for a caller that has no sidecar, and
    # falls back to `sku` — which is right for the only mode where a row IS
    # an instrument.
    old_by_sku, old_unmatchable = _by_sku(old, mode)
    new_by_sku, new_unmatchable = _by_sku(new, mode)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    # NO `lifecycle` bucket, and its absence is a decision with a reason
    # rather than an omission. A sibling repo needs one because its site
    # rotates ads in and out of a promoted slot that appears in the rows, so
    # a placement move would otherwise read as a price change. Google Finance also
    # sells placement — 7 of 52 payload entries on one measured page — but
    # those sponsored entries never become rows at all: they carry a
    # click-tracking redirect instead of a product URL and `parse_products`
    # drops them. So there is no placement column for a lifecycle bucket to
    # key on, and porting one would be dead code that looks load-bearing
    # (§4).
    changed, source_changed, within_tolerance = [], [], []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue

        # A row whose price_source differs between runs is not comparable on
        # price: here that means one run had its structured price confirmed
        # against a rendered tile ("jsonld+dom") while the other did not
        # ("jsonld"), or fell back to reading the DOM alone ("dom"). The
        # figures should agree, and when they do not, the difference is in
        # how OUR two snapshots rendered, not in what the shop charges.
        # Reporting it as a price change would be a false alarm about the
        # site. Non-price fields still compare fine.
        sources = (before.get("price_source"), after.get("price_source"))
        if sources[0] != sources[1] and any(f in field_changes for f in PRICE_FIELDS):
            price_part = {f: v for f, v in field_changes.items() if f in PRICE_FIELDS}
            other_part = {f: v for f, v in field_changes.items() if f not in PRICE_FIELDS}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                "price_source": {"old": sources[0], "new": sources[1]},
                "changes": price_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        # An FX tick rather than a price change — see _within_tolerance. Only
        # when the ONLY differences are price fields: a currency or stock
        # change alongside is a real change whatever the size of the move.
        if (all(f in PRICE_FIELDS for f in field_changes)
                and _within_tolerance(before, after, field_changes,
                                      price_tolerance_pct)):
            within_tolerance.append({"sku": sku, "title": after.get("title"),
                                     "changes": field_changes})
            continue

        changed.append({"sku": sku, "title": after.get("title"),
                        "changes": field_changes})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "within_tolerance": within_tolerance,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result['source_changed'])} not comparable on price, "
          f"{len(result.get('within_tolerance', []))} within the price "
          f"tolerance.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result.get("within_tolerance", []):
        moves = ", ".join(
            f"{f}: {v['old']} -> {v['new']}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {moves}  [within --price-"
              f"tolerance-pct: an exchange-rate tick, not a price change]")
    for c in result["source_changed"]:
        src = c["price_source"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[price_source {src['old']!r} -> {src['new']!r}: the two runs "
              f"rendered differently, so this is not a site-side price change]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read the `<out>.meta.json` sidecar beside a run's JSON output.

    Returns (status, meta), or (None, None) when there is no sidecar — which
    is the normal case for output written before run metadata existed, or by
    `scraper_api_client.py` (single fetch, no pagination to cut short).
    """
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _check_comparable(args) -> bool:
    """Refuse an assortment diff between runs that are not both complete.

    This is the failure mode the sidecar exists for: a run cut short on page
    3 of 10 is missing every product on pages 4-10, and diffing it against
    yesterday's full run reports all of them as `removed` — reading as "these
    products were delisted" when in fact they were simply never fetched.
    Prices of the SKUs both runs DID see are still comparable, which is why
    this is a refusal with a --force escape hatch rather than a hard error.
    """
    problems = []
    modes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            continue  # no sidecar: nothing to check, see _run_status
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        if mode and mode not in UNIQUE_BY_SKU_MODES \
                and mode not in DEDUPE_KEY_BY_MODE:
            # This tool's premise is one identifiable row per instrument,
            # diffed on price. `quote` is one row per sku; `markets` and
            # `movers` are one row per (sku, listing), which is still one
            # row per identity and is therefore comparable — the key just
            # has two parts. A mode with NEITHER property would give a diff
            # whose every line is an artefact of two rows sharing an id, so
            # it is refused outright rather than answered. The check is here
            # so that adding such a mode is caught rather than discovered.
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which is not one row "
                f"per instrument. This tool diffs one identified row on "
                f"price, so there is nothing here it can compare.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        problems.append(
            f"the two runs are different modes ({modes}). A quote row and a "
            f"list row carry different fields, so `added`/`removed` would "
            f"describe the mode change rather than the market.")

    # A MARKET MISMATCH, which is this site's version of the sibling repos'
    # cross-storefront refusal — and unlike their hostname split, it is
    # invisible in the data.
    #
    # The market page's lists are GEO-SELECTED: measured 2026-09-22 from one
    # address, `gl=US` returned CBOE sector indices and NASDAQ movers while
    # `gl=DE` returned STOXX and ETR, with the two sector sets not
    # overlapping at all. So a US run diffed against a DE run reports every
    # row as `added` and every row as `removed`, and every line of that diff
    # is an artefact of the parameter rather than a fact about any market.
    #
    # Both runs' `source` is "google.com/finance", so nothing upstream
    # catches this. The sidecar's `market` is the only place the difference
    # is stated, which is why it is written unconditionally.
    markets = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        _status, meta = _run_status(path)
        if meta and meta.get("market") is not None:
            markets[label] = meta["market"]
    if len(set(markets.values())) > 1:
        problems.append(
            f"the two runs read different markets ({markets}). The market "
            f"page's lists are geo-selected, so these are two SAMPLES rather "
            f"than a before and an after — every row would report as both "
            f"added and removed. Compare a market against itself, or pass "
            f"--force if you know what you are asking for.")

    # A CURRENCY CHANGE UNDER ONE INSTRUMENT.
    #
    # The sibling this was inherited from has ONE currency per run and
    # treats a second as proof the run was redirected mid-way. That premise
    # is false here and the check was actively misleading because of it: a
    # single `--mode markets` run legitimately holds USD, EUR, JPY, GBP and
    # more at once, because the market page publishes FX pairs, crypto,
    # futures and indices side by side. Running the real tool on a real
    # markets run produced "holds more than one currency (['CAD','JPY',
    # 'USD']) — that run was redirected mid-way", which is a false alarm
    # with a false explanation attached.
    #
    # What IS worth catching is an instrument whose currency changed between
    # two runs: GOOGL quoted in USD yesterday and EUR today means one of the
    # two runs read the wrong venue, and every price comparison for that row
    # is meaningless. So the check is per-sku, across the two runs, rather
    # than per-run.
    by_sku = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        try:
            rows = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for r in rows:
            sku, cur = r.get("sku"), r.get("currency")
            if sku and cur:
                by_sku.setdefault(sku, {})[label] = cur
    moved = {s: v for s, v in by_sku.items()
             if len(v) == 2 and v["--old"] != v["--new"]}
    if moved:
        sample = ", ".join("%s %s->%s" % (s, v["--old"], v["--new"])
                           for s, v in list(moved.items())[:3])
        problems.append(
            f"{len(moved)} instrument(s) changed currency between the two "
            f"runs ({sample}). An instrument does not change the currency "
            f"its venue quotes in, so one of these runs read a different "
            f"venue and every price comparison for those rows is "
            f"meaningless. `source` cannot catch it: it is "
            f"'google.com/finance' on both sides.")

    if not problems:
        return True

    # A generic headline, because the reasons below are no longer only about
    # completeness: a mode mismatch and a reviews run are refused too, and a
    # message naming the wrong reason sends the reader looking in the wrong
    # place.
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare anyway "
          "(added/removed will include products that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two google-finance-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--price-tolerance-pct", type=float, default=0.0,
                   metavar="PCT",
                   help="Treat a price move smaller than PCT%% as an exchange-"
                        "rate tick rather than a price change: reported "
                        "separately and ignored by --fail-on-change. Default "
                        "0, which is what a Google Finance run wants for two "
                        "reasons: the site quotes JPY to every visitor so "
                        "there is no conversion drift to absorb, and the yen "
                        "has no subunit, so every price is a whole number and "
                        "there is no rounding tick either. The flag is "
                        "inherited from this scraper family; set it non-zero "
                        "only with a reason you can state.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was partial "
                        "or failed. Products never fetched by the short run will "
                        "appear as added/removed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    # The mode comes from the sidecar rather than a flag: it is what the
    # run itself recorded, so a caller cannot tell the differ the wrong one.
    _status, _meta = _run_status(args.new)
    mode = (_meta or {}).get("mode")
    result = diff_products(old, new, price_tolerance_pct=args.price_tolerance_pct,
                           mode=mode)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # Neither `source_changed` nor `within_tolerance` is a reason to fail.
    # The first means our own two snapshots rendered differently; the second
    # means an exchange rate moved. Neither says anything about the site, and
    # alerting on either would train whoever reads the alert to ignore it.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
