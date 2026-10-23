"""Local web front end for the FFXIV crafting-profit report.

Serves data/profit_data.json (cost region-wide, revenue/velocity per scope)
as a sortable table. Cost is always region-wide; revenue/profit/margin/
sales-per-day reflect either the combined NA region or a single selected
world, per the `world` query param. Also lets the user trigger a fresh
price pull (fetch_prices.py + compute_profit.py) without leaving the
browser. Recipe data (fetch_recipes.py) is retained
unless quality capability metadata is missing, in which case recipes are
updated once before fetching market prices.
"""
import json
import math
import os
import shutil
import tempfile
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from ffxiv_data import NA_DATA_CENTERS
from compute_profit import MIN_SALES_PER_DAY
from quality import COST_SCHEMA
from refresh_publish import publish_refresh
from insights import describe_estimate
from market_validation import valid_coverage
from report_format import quality_option, validate_quality_references
from pagination import table_query, paginate

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR.parent / "data"
PROFIT_DATA_PATH = DATA_DIR / "profit_data.json"
PROGRESS_PATH = DATA_DIR / "fetch_progress.json"

app = Flask(__name__)

refresh_state = {
    "status": "idle",  # idle | running | done | error
    "step": "",
    "error": None,
    "started_at": None,
    "finished_at": None,
}
refresh_lock = threading.Lock()

_cache = {"mtime": None, "rows": None, "signature": None, "error": None}
cache_lock = threading.Lock()

ICON_BASE = "https://v2.xivapi.com/api/asset"


def icon_url(icon_path):
    if not icon_path:
        return None
    return f"{ICON_BASE}?path={icon_path}&format=png"


def get_fetch_progress():
    try:
        return json.loads(PROGRESS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def validate_report_rows(rows):
    """Check the structures the data endpoint reads before accepting a snapshot."""
    def number(value):
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)):
            raise ValueError("Invalid numeric value in report")

    def sales_sample(sample):
        if sample is None:
            return
        if not isinstance(sample, dict):
            raise ValueError("Malformed sales sample")
        if 'data_centers' in sample:
            if not isinstance(sample['data_centers'], dict):
                raise ValueError("Malformed data center samples")
            for child in sample['data_centers'].values():
                sales_sample(child)
        for key in ('transactions', 'units', 'window_days', 'snapshot_at'):
            number(sample.get(key))

    if not isinstance(rows, list) or not rows:
        raise ValueError("Report has no item rows")
    for row in rows:
        if not isinstance(row, dict) or not all(key in row for key in ('item', 'item_id', 'crafter_job', 'level', 'yield_qty', 'scopes')):
            raise ValueError("Malformed item row")
        if not isinstance(row['item'], str) or not isinstance(row['crafter_job'], str) or not isinstance(row['scopes'], dict):
            raise ValueError("Malformed item details")
        for key in ('item_id', 'level', 'yield_qty'):
            number(row[key])
            if row[key] is None:
                raise ValueError("Missing item number")
        if row.get('refresh_warning') is not None and not isinstance(row['refresh_warning'], str):
            raise ValueError('Malformed saved refresh warning')
        if not valid_coverage(row.get('market_coverage', {})):
            raise ValueError('Malformed market coverage')
        costs = row.get('cost_options', {})
        if not isinstance(costs, dict):
            raise ValueError("Malformed ingredient costs")
        for cost in costs.values():
            if not isinstance(cost, dict) or not isinstance(cost.get('ingredients', []), list):
                raise ValueError("Malformed ingredient cost option")
            number(cost.get('cost'))
            for ing in cost.get('ingredients', []):
                if not isinstance(ing, dict) or not all(key in ing for key in ('item_id', 'name', 'quantity', 'unit_price', 'subtotal')):
                    raise ValueError("Malformed ingredient row")
                if not valid_coverage(ing.get('market_coverage', {})):
                    raise ValueError('Malformed ingredient market coverage')
                if not isinstance(ing['name'], str):
                    raise ValueError("Malformed ingredient name")
                for key in ('item_id', 'quantity', 'unit_price', 'subtotal', 'source_updated_at', 'last_sale_at'):
                    number(ing.get(key))
                sample = ing.get('sample_listings', [])
                if not isinstance(sample, list):
                    raise ValueError("Malformed ingredient price sample")
                for listing in sample:
                    if not isinstance(listing, dict) or not isinstance(listing.get('quality'), str) or not isinstance(listing.get('world'), str):
                        raise ValueError("Malformed ingredient sample listing")
                    number(listing.get('quantity'))
                    number(listing.get('unit_price'))
                    number(listing.get('source_updated_at'))
        for scope in row['scopes'].values():
            if not isinstance(scope, dict) or not isinstance(scope.get('quality_options', {}), dict):
                raise ValueError("Malformed selling scope")
            validate_quality_references(scope)
            for option in scope.get('quality_options', {}).values():
                if isinstance(option, str):
                    continue
                if not isinstance(option, dict):
                    raise ValueError("Malformed quality option")
                for key in ('revenue', 'sale_velocity_per_day', 'current_unit_price', 'historical_unit_price', 'source_updated_at', 'last_sale_at'):
                    number(option.get(key))
                if 'source_time_incomplete' in option and not isinstance(option['source_time_incomplete'], bool):
                    raise ValueError('Malformed source timestamp coverage')
                sales_sample(option.get('sales_sample'))
                rates = option.get('quality_rates', {})
                if not isinstance(rates, dict):
                    raise ValueError("Malformed quality sales rates")
                for rate in rates.values():
                    number(rate)


def report_signature(stat):
    # Windows stat/fstat disagree about ctime semantics. File identity also
    # detects atomic replacement when the new report has equal size and mtime.
    return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)


def get_rows():
    # Readers see one complete cache snapshot. A failed load never replaces it.
    with cache_lock:
        try:
            stat = PROFIT_DATA_PATH.stat()
            signature = report_signature(stat)
            if _cache.get('signature') != signature or _cache['rows'] is None:
                with PROFIT_DATA_PATH.open(encoding='utf-8') as report:
                    contents = report.read()
                    stat = os.fstat(report.fileno())
                    signature = report_signature(stat)
                rows = json.loads(contents)
                del contents
                validate_report_rows(rows)
                _cache.update(rows=rows, mtime=stat.st_mtime, signature=signature)
            _cache['error'] = None
        except (OSError, UnicodeError, ValueError) as exc:
            _cache['error'] = f"Unable to load the saved report: {exc}"
        return _cache['rows'] if _cache['rows'] is not None else [], _cache['mtime']


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/worlds")
def api_worlds():
    return jsonify({"data_centers": NA_DATA_CENTERS})


@app.route("/api/data")
def api_data():
    try:
        query = table_query(request.args)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    dataset = request.args.get("dataset", "liquid")
    world = request.args.get("world", "region")
    product_quality = request.args.get("product_quality", "auto")
    ingredient_quality = request.args.get("ingredient_quality", "cheapest")
    if product_quality not in {"auto", "nq", "hq"} or ingredient_quality not in {"cheapest", "nq", "hq"}:
        return jsonify(error="Invalid quality filter"), 400
    try:
        fee_pct = float(request.args.get("fee_pct", "0"))
        if not math.isfinite(fee_pct) or not 0 <= fee_pct <= 100:
            raise ValueError()
    except ValueError:
        return jsonify(error="Selling-fee allowance must be between 0 and 100 percent"), 400
    rows, mtime = get_rows()
    with cache_lock:
        data_error = _cache.get("error")
    if data_error and mtime is None:
        return jsonify(error="No usable report is available. Refresh prices to build one.",
                       data_warning=data_error, rows=[], count=0, updated_at=None), 503
    refresh_warnings = list(dict.fromkeys(r.get('refresh_warning') for r in rows if r.get('refresh_warning')))
    legacy = any(r.get("quality_schema") != 1 or r.get("cost_schema") != COST_SCHEMA for r in rows)

    result = []
    for r in rows:
        scope = r["scopes"].get(world)
        if scope is None:
            continue
        if product_quality == "hq" and r.get("can_be_hq") is False:
            continue
        if r.get("quality_schema") == 1 and r.get("cost_schema") == COST_SCHEMA:
            scope = quality_option(scope, product_quality)
            cost_option = r.get("cost_options", {}).get(ingredient_quality, {}) if r.get("cost_schema") == COST_SCHEMA else {}
        else:
            # Old snapshots cannot answer quality-specific questions safely.
            scope = {}
            cost_option = {}
        cost = cost_option.get("cost")
        gross_revenue = scope.get("revenue")
        revenue = gross_revenue * (1 - fee_pct / 100) if gross_revenue is not None else None
        profit = revenue - cost if revenue is not None and cost is not None else None
        margin_pct = profit / cost * 100 if profit is not None and cost else None
        velocity = scope.get("sale_velocity_per_day")
        unavailable = []
        if revenue is None:
            unavailable.append("Insufficient product quality data")
        if cost is None:
            unavailable.append("Insufficient observed ingredient supply, missing listing quantities, or unknown quality capability")
        if dataset == "liquid" and (velocity is None or velocity < MIN_SALES_PER_DAY):
            continue
        result.append({
            "item": r["item"],
            "item_id": r["item_id"],
            "icon_url": icon_url(r.get("icon_path")),
            "crafter_job": r["crafter_job"],
            "level": r["level"],
            "yield_qty": r["yield_qty"],
            "cost": cost,
            "revenue": round(revenue, 2) if revenue is not None else None,
            "gross_revenue": gross_revenue,
            "fee_pct": fee_pct,
            "daily_profit_potential": round(profit * velocity / r["yield_qty"], 1) if profit is not None and velocity is not None and r["yield_qty"] > 0 else None,
            "market_url": f"https://universalis.app/market/{int(r['item_id'])}",
            "market_coverage": r.get("market_coverage", {}),
            "source_updated_at": scope.get("source_updated_at"),
            "source_time_incomplete": scope.get("source_time_incomplete", True),
            "last_sale_at": scope.get("last_sale_at"),
            "profit": round(profit, 1) if profit is not None else None,
            "product_quality": scope.get("product_quality"),
            "quality_rates": scope.get("quality_rates", {}),
            "price_source": scope.get("price_source"),
            "price_difference": scope.get("price_difference", False),
            "current_unit_price": scope.get("current_unit_price"),
            "historical_unit_price": scope.get("historical_unit_price"),
            "cost_historical": any(i.get("price_source") == "historical" for i in cost_option.get("ingredients", [])),
            "cost_limited_supply": any(i.get("price_source") == "current" and i.get("limited_supply") for i in cost_option.get("ingredients", [])),
            "unavailable_reason": "; ".join(unavailable),
            "margin_pct": round(margin_pct, 1) if margin_pct is not None else None,
            "sale_velocity_per_day": velocity,
            "sales_sample": scope.get("sales_sample"),
            "ingredients": cost_option.get("ingredients", []),
        })

    now = time.time()
    for row in result:
        row.update(describe_estimate(row, now))
    result, pagination = paginate(result, query)
    # Only prepare tooltip payloads for the selected page.
    for row in result:
        row["ingredients"] = [
            {
                "item_id": ing["item_id"],
                "name": ing["name"],
                "icon_url": icon_url(ing.get("icon_path")),
                "quantity": ing["quantity"],
                "quality": ing.get("quality"),
                "unit_price": ing["unit_price"],
                "subtotal": ing["subtotal"],
                "market_coverage": ing.get("market_coverage", {}),
                "source_updated_at": ing.get("source_updated_at"),
                "last_sale_at": ing.get("last_sale_at"),
                "price_source": ing.get("price_source"),
                "limited_supply": ing.get("limited_supply", False),
                "observed_quantity": ing.get("observed_quantity"),
                "historical_transactions": ing.get("historical_transactions"),
                "sample_quantity": ing.get("sample_quantity"),
                "sample_listings": ing.get("sample_listings", []),
            }
            for ing in row["ingredients"]
        ]
    return jsonify({"rows": result, **pagination, "updated_at": mtime, "dataset": dataset, "world": world,
                    "data_warning": " ".join((["Showing the last successfully loaded report. " + data_error] if data_error else []) + refresh_warnings) or None,
                    "refresh_warning": " ".join(refresh_warnings) or None,
                    "quality_refresh_required": legacy,
                    "product_quality": product_quality, "ingredient_quality": ingredient_quality})


def _run_refresh():
    try:
        PROGRESS_PATH.unlink(missing_ok=True)  # clear any stale progress from a previous run

        DATA_DIR.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="refresh-", dir=DATA_DIR) as directory:
            staging = Path(directory)
            recipe_path = DATA_DIR / "recipes.json"
            recipes = json.loads(recipe_path.read_text()) if recipe_path.exists() else []
            environment = dict(os.environ, FFXIV_DATA_DIR=str(staging), FFXIV_PROGRESS_PATH=str(PROGRESS_PATH),
                               FFXIV_CACHE_PATH=str(DATA_DIR / 'market_cache.sqlite3'))
            if (DATA_DIR / 'prices.json').exists():
                shutil.copy2(DATA_DIR / 'prices.json', staging / 'prices.json')

            def run_step(script, step, timeout):
                with refresh_lock:
                    refresh_state["step"] = step
                result = subprocess.run([sys.executable, str(APP_DIR / script)],
                                        cwd=str(APP_DIR), capture_output=True, text=True,
                                        timeout=timeout, env=environment)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr[-2000:] or f"{script} failed")

            if not recipes or any("can_be_hq" not in r or any("can_be_hq" not in i for i in r["ingredients"]) for r in recipes):
                run_step("fetch_recipes.py", "updating recipe quality metadata", 600)
            else:
                shutil.copy2(recipe_path, staging / "recipes.json")
            run_step("fetch_prices.py", "fetching prices (4 NA data centers)", 7200)
            summary_path = staging / 'refresh_summary.json'
            summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
            run_step("compute_profit.py", "computing profit", 300)
            with refresh_lock:
                refresh_state["step"] = "validating and publishing refresh"
            publish_refresh(staging, DATA_DIR)

        with refresh_lock:
            refresh_state.update(status="done", step="", warning=summary.get('warning'), finished_at=time.time())
    except Exception as e:
        with refresh_lock:
            refresh_state.update(status="error", step="", error=str(e), finished_at=time.time())


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    with refresh_lock:
        if refresh_state["status"] == "running":
            return jsonify(refresh_state), 409
        refresh_state.update(status="running", step="fetching prices (4 NA data centers)", error=None, warning=None, started_at=time.time(), finished_at=None)
        try:
            PROGRESS_PATH.unlink(missing_ok=True)
            thread = threading.Thread(target=_run_refresh, daemon=True)
            thread.start()
        except Exception as exc:
            refresh_state.update(status="error", step="", error=str(exc), finished_at=time.time())
            return jsonify(refresh_state), 500
        state = dict(refresh_state)
    return jsonify(state), 202


@app.route("/api/refresh/status")
def api_refresh_status():
    with refresh_lock:
        state = dict(refresh_state)
    if state["status"] == "running":
        state["progress"] = get_fetch_progress()
    return jsonify(state)


if __name__ == "__main__":
    app.run(debug=True, port=5057)
