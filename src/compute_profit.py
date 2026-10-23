"""Join recipes with market prices and produce a profit dataset spanning
the whole NA region and every individual NA world.

Cost is always computed region-wide, as data center travel allows purchasing
ingredients in any world. Revenue is kept per-scope because selling only
happens whichever market you actually list on.
data/profit_data.json stores cost once per item plus a revenue/
sale_velocity pair per scope, so the front end can compute profit/margin
for whatever scope (region or a single selected world) the user picks
without re-running this script.

Revenue uses current listings, or labeled historical medians when listings are absent.
Ingredient costs estimate materials consumed using a small quantity-weighted
sample of cheap current listings, without charging for unused stack units.

Also writes data/profit_report.csv / profit_report_liquid.csv as a flat,
region-only view for quick viewing outside the app.
"""
import csv
import json
import os
from pathlib import Path

from ffxiv_data import JOB_ABBREVIATIONS
from file_io import prepared_output, replace_with_retry
from market_validation import coverage_metadata
from quality import COST_SCHEMA, IngredientEstimates, product_options, ingredient_options
from report_format import compact_row, cost_option, quality_option

DATA_DIR = Path(os.environ.get("FFXIV_DATA_DIR", Path(__file__).parent.parent / "data"))
RECIPES_PATH = DATA_DIR / "recipes.json"
PRICES_PATH = DATA_DIR / "prices.json"
PROFIT_DATA_PATH = DATA_DIR / "profit_data.json"
CSV_PATH = DATA_DIR / "profit_report.csv"
CSV_LIQUID_PATH = DATA_DIR / "profit_report_liquid.csv"

MIN_SALES_PER_DAY = 0.5


def main():
    recipes = json.loads(RECIPES_PATH.read_text())
    prices = json.loads(PRICES_PATH.read_text())

    if any("qualities" not in entry.get("region", {}) for entry in prices.values()):
        raise ValueError("Quality-separated market data is missing; run fetch_prices.py before computing profit")

    if any(not isinstance(entry.get("region", {}).get("purchase_listings"), list) for entry in prices.values()):
        raise ValueError("Listing quantities are missing; run fetch_prices.py before computing profit")

    summary_path = PRICES_PATH.parent / 'refresh_summary.json'
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    if not isinstance(summary, dict) or (summary.get('warning') is not None and not isinstance(summary['warning'], str)):
        raise ValueError('Invalid refresh summary; existing reports were preserved')

    rows = []
    estimates = IngredientEstimates(prices)
    for r in recipes:
        result_entry = prices.get(str(r["result_item_id"]))
        if result_entry is None:
            continue
        costs = ingredient_options(r, prices, estimates=estimates)
        scopes = {}
        for name, stats in [("region", result_entry["region"]), *result_entry.get("worlds", {}).items()]:
            options = product_options(stats, r.get("can_be_hq"))
            choices = {q: dict(
                revenue=round(option["unit_price"] * r["amount_result"], 1),
                sale_velocity_per_day=option["sale_velocity_per_day"],
                sales_sample=option.get("sales_sample"), product_quality=option["product_quality"],
                price_source=option["price_source"], price_difference=option["price_difference"],
                source_updated_at=option.get("source_updated_at"), source_time_incomplete=option.get("source_time_incomplete", True),
                last_sale_at=option.get("last_sale_at"),
                current_unit_price=option["current_unit_price"], historical_unit_price=option["historical_unit_price"],
                quality_rates={q: stats["qualities"][q]["sale_velocity_per_day"] for q in ("nq", "hq") if q in stats.get("qualities", {})},
            ) for q, option in options.items()}
            scopes[name] = {'quality_options': choices}
        rows.append(compact_row({
            "item": r["result_name"],
            "item_id": r["result_item_id"],
            "icon_path": r.get("icon_path"),
            "crafter_job": JOB_ABBREVIATIONS.get(r["crafter_job"], r["crafter_job"]),
            "level": r["level"],
            "yield_qty": r["amount_result"],
            "cost_options": costs,
            "can_be_hq": r.get("can_be_hq"),
            "quality_schema": 1,
            "cost_schema": COST_SCHEMA,
            "refresh_warning": summary.get('warning'),
            "refresh_assessed_at": summary.get('assessed_at'),
            "market_coverage": coverage_metadata(result_entry, summary.get("max_age_hours", 12)),
            "scopes": scopes,
        }))

    if not rows or not any(cost_option(r).get('cost') is not None and any(
            'revenue' in quality_option(scope) for scope in r['scopes'].values()) for r in rows):
        raise ValueError("No usable crafting estimates; existing reports were preserved")

    with prepared_output(PROFIT_DATA_PATH) as (stream, temporary):
        # Encode a row at a time: avoid tiny writes without a second report-sized string.
        stream.write('[')
        for index, row in enumerate(rows):
            if index:
                stream.write(',')
            stream.write(json.dumps(row, separators=(',', ':')))
        stream.write(']')
    print(f"Prepared {len(rows)} craftable items in {temporary}", flush=True)

    write_flat_csv(rows)
    replace_with_retry(temporary, PROFIT_DATA_PATH)
    print(f"Saved {len(rows)} craftable items to {PROFIT_DATA_PATH}", flush=True)


def write_flat_csv(rows):
    flat = []
    for r in rows:
        region = quality_option(r['scopes']['region'])
        costs = cost_option(r)
        cost = costs.get('cost')
        if cost is None or "revenue" not in region:
            continue
        profit = region['revenue'] - cost
        margin_pct = (profit / cost * 100) if cost > 0 else None
        flat.append({
            "item": r["item"],
            "item_id": r["item_id"],
            "crafter_job": r["crafter_job"],
            "level": r["level"],
            "yield_qty": r["yield_qty"],
            "cost": cost,
            "revenue": region["revenue"],
            "profit": round(profit, 1),
            "margin_pct": round(margin_pct, 1) if margin_pct is not None else "",
            "sale_velocity_per_day": region["sale_velocity_per_day"],
            "daily_profit_potential": round(profit * region["sale_velocity_per_day"] / r["yield_qty"], 1) if r["yield_qty"] > 0 else None,
            "fee_pct": 0,
            "source_updated_at": region.get("source_updated_at"),
            "refresh_warning": r.get("refresh_warning"),
            "market_coverage": json.dumps(r.get("market_coverage", {}), sort_keys=True),
            "market_url": f"https://universalis.app/market/{r['item_id']}",
            "price_source": region.get("price_source"),
            "price_difference": region.get("price_difference", False),
            "historical_unit_price": region.get("historical_unit_price"),
            "cost_historical": any(i.get("price_source") == "historical" for i in costs.get('ingredients', [])),
            "cost_limited_supply": any(i.get("price_source") == "current" and i.get("limited_supply") for i in costs.get('ingredients', [])),
        })

    flat.sort(key=lambda x: x["profit"], reverse=True)
    with prepared_output(CSV_PATH, newline="") as (f, csv_temporary):
        writer = csv.DictWriter(f, fieldnames=["item", "item_id", "crafter_job", "level", "yield_qty", "cost", "revenue", "profit", "margin_pct", "sale_velocity_per_day", "price_source", "price_difference", "historical_unit_price", "cost_historical", "cost_limited_supply", "daily_profit_potential", "fee_pct", "source_updated_at", "market_url", "refresh_warning", "market_coverage"])
        writer.writeheader()
        writer.writerows(flat)
    replace_with_retry(csv_temporary, CSV_PATH)
    print(f"Wrote {len(flat)} items (region view) to {CSV_PATH}")

    liquid = [r for r in flat if r["sale_velocity_per_day"] >= MIN_SALES_PER_DAY]
    with prepared_output(CSV_LIQUID_PATH, newline="") as (f, csv_temporary):
        writer = csv.DictWriter(f, fieldnames=["item", "item_id", "crafter_job", "level", "yield_qty", "cost", "revenue", "profit", "margin_pct", "sale_velocity_per_day", "price_source", "price_difference", "historical_unit_price", "cost_historical", "cost_limited_supply", "daily_profit_potential", "fee_pct", "source_updated_at", "market_url", "refresh_warning", "market_coverage"])
        writer.writeheader()
        writer.writerows(liquid)
    replace_with_retry(csv_temporary, CSV_LIQUID_PATH)
    print(f"Wrote {len(liquid)} items selling >= {MIN_SALES_PER_DAY}/day (region view) to {CSV_LIQUID_PATH}")

    header = f"{'Item':<35} {'Job':<13} {'Lvl':>3} {'Cost':>8} {'Rev':>8} {'Profit':>8} {'Margin%':>8} {'Sales/day':>10}"

    def fmt(row):
        return (f"{row['item']:<35} {row['crafter_job']:<13} {row['level']:>3} "
                f"{row['cost']:>8} {row['revenue']:>8} {row['profit']:>8} {row['margin_pct']!s:>8} {row['sale_velocity_per_day']:>10}")

    print(f"\nTop 15 by profit (region, selling >= {MIN_SALES_PER_DAY}/day):")
    print(header)
    for row in liquid[:15]:
        print(fmt(row))


if __name__ == "__main__":
    main()
