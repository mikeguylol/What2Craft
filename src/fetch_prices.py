"""Fetch current Universalis market data for every item referenced by
data/recipes.json, across all 4 NA data centers.

Universalis's DC-scoped endpoint (world param = a data center name instead
of a single world) returns listings and recent sale history tagged with
each entry's worldName, so a single set of requests per DC gives us both
a whole-DC view and a per-world breakdown. We pull raw listings/recent
sales (entries=200 per item per DC) and compute price/velocity,
separately per data center before summing regional rates. Worlds use
their parent data center sample window, rather than their own oldest sale.

Saves data/prices.json:
{
  "<item_id>": {
    "region": {min_price_nq, min_price_any, avg_price, sale_velocity_per_day},
    "worlds": {
      "<world_name>": {min_price_nq, min_price_any, avg_price, sale_velocity_per_day},
      ...  # only worlds with at least one listing or recent sale are included
    }
  }
}
"""
import json
import os
import math
import statistics
import sys
import time
import random
import threading
import uuid
import tempfile
import argparse
from contextlib import suppress
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, CancelledError
from pathlib import Path

import requests

from ffxiv_data import NA_DATA_CENTERS
from market_cache import CacheWriteError, load_samples, save_samples
from market_validation import compatible_price

BATCH_SIZE = 10  # Live probes: 16/16 valid across NA; larger batches returned 500/504.
LISTING_ENTRIES = 100  # Preserve a broader observed stack sample for ingredient purchases.
HISTORY_ENTRIES = 200  # recent sales pulled per item per DC, used to compute our own velocity.
# Higher than the ~5 default matters a lot here: on a busy DC, a high-population world's sales
# can crowd a quieter world's own recent sales out of a small shared window entirely.
MAX_WORKERS = 4  # Below Universalis's eight-connection limit.
_request_lock = threading.Lock()
_next_request = 0.0


def pace_request():
    global _next_request
    with _request_lock:
        time.sleep(max(0, _next_request - time.monotonic()))
        _next_request = time.monotonic() + 0.2  # at most 5 starts/second


def atomic_json(path, value):
    # A reader can briefly prevent replacement on Windows. Unique temporary
    # names also prevent concurrent writers from replacing each other's data.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream)
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * 2 ** attempt)
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
MIN_SPAN_DAYS = 1 / 24  # floor the velocity time window at 1 hour to avoid divide-by-near-zero spikes
DEMAND_LOOKBACK_DAYS = 30

DATA_DIR = Path(os.environ.get("FFXIV_DATA_DIR", Path(__file__).parent.parent / "data"))
RECIPES_PATH = DATA_DIR / "recipes.json"
OUT_PATH = DATA_DIR / "prices.json"
PROGRESS_PATH = Path(os.environ.get("FFXIV_PROGRESS_PATH", DATA_DIR / "fetch_progress.json"))
# Web refreshes use a temporary data directory, but their logs must survive
# that directory's cleanup (including failed runs).
REQUEST_LOG_DIR = PROGRESS_PATH.parent / "request_logs"
_log_lock = threading.Lock()
_run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
REQUEST_LOG_PATH = REQUEST_LOG_DIR / f'fetch_prices-{_run_id}.jsonl'
_progress_warning = False
_log_warning = False


def log_request_event(event):
    """Append and flush one event so in-flight requests survive an interrupted run."""
    record = {"run_id": _run_id, "timestamp": datetime.now(timezone.utc).isoformat(), **event}
    global _log_warning
    with _log_lock:
        try:
            REQUEST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with REQUEST_LOG_PATH.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record) + '\n')
            _log_warning = False
        except OSError as exc:
            if not _log_warning:
                print(f'Warning: request log unavailable; fetching continues: {exc}', file=sys.stderr, flush=True)
            _log_warning = True


def request_market_data(dc, ids_param, attempt):
    """Log the exact request plus its transport outcome; validation is logged separately."""
    url = f'https://universalis.app/api/v2/{dc}/{ids_param}'
    params = {"entries": HISTORY_ENTRIES, "listings": LISTING_ENTRIES}
    request_id = uuid.uuid4().hex
    context = {"request_id": request_id, "data_center": dc,
               "item_ids": list(map(int, ids_param.split(','))), "attempt": attempt,
               "method": "GET", "url": url, "params": params, "timeout_seconds": [10, 60]}
    log_request_event({**context, "event": "request_started"})
    started = time.monotonic()
    response = None
    try:
        response = requests.get(url, params=params, timeout=(10, 60))
        response._debug_request_id = request_id
        log_request_event({**context, "event": "response_received", "status_code": response.status_code,
                           "elapsed_seconds": round(time.monotonic() - started, 3),
                           "response_bytes": len(response.content),
                           "retry_after": response.headers.get('Retry-After')})
        return response
    except requests.exceptions.RequestException as exc:
        log_request_event({**context, "event": "request_error", "error_type": type(exc).__name__,
                           "error": str(exc), "elapsed_seconds": round(time.monotonic() - started, 3)})
        raise


def write_progress(done, total, failed=0):
    global _progress_warning
    try:
        atomic_json(PROGRESS_PATH, {"done": done, "total": total, "failed": failed,
                                    "updated_at": time.time(), "run_id": _run_id})
        if _progress_warning:
            log_request_event({"event": "progress_write_recovered", "done": done, "total": total})
        _progress_warning = False
    except OSError as exc:
        if not _progress_warning:
            print(f'Warning: progress file unavailable; fetching and result collection continue: {exc}',
                  file=sys.stderr, flush=True)
            log_request_event({"event": "progress_write_failed", "done": done, "total": total, "error": str(exc)})
        _progress_warning = True


def collect_item_ids(recipes):
    ids = set()
    for r in recipes:
        ids.add(r["result_item_id"])
        for ing in r["ingredients"]:
            ids.add(ing["item_id"])
    return sorted(ids)


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def validate_market_payload(payload, expected):
    """HTTP success and covered IDs do not establish valid market data."""
    if not isinstance(payload, dict):
        raise ValueError("Expected a market-data object")
    if len(expected) == 1:
        if str(payload.get("itemID")) not in expected:
            raise ValueError("Incorrect item ID")
        items, unresolved = {next(iter(expected)): payload}, set()
    else:
        items = payload.get("items")
        missing = payload.get("unresolvedItems", [])
        if not isinstance(items, dict) or not isinstance(missing, list):
            raise ValueError("Invalid items or unresolvedItems")
        unresolved = set(map(str, missing))
        if set(items) & unresolved or set(items) | unresolved != expected:
            raise ValueError("Incomplete or unexpected item coverage")
    for item_id, entry in items.items():
        if not isinstance(entry, dict) or str(entry.get("itemID")) != item_id:
            raise ValueError("Invalid item data or mismatched item ID")
        for field in ("listings", "recentHistory"):
            records = entry.get(field)
            if not isinstance(records, list):
                raise ValueError(f"Missing or invalid {field} for item {item_id}")
            for record in records:
                if (not isinstance(record, dict) or not isinstance(record.get("worldName"), str)
                        or not record["worldName"] or not isinstance(record.get("hq"), bool)):
                    raise ValueError(f"Invalid {field} record for item {item_id}")
                for key in ("pricePerUnit", "quantity") + (("timestamp",) if field == "recentHistory" else ()):
                    value = record.get(key)
                    if (not isinstance(value, (int, float)) or isinstance(value, bool)
                            or not math.isfinite(value) or value < (0 if key == "pricePerUnit" else 1)
                            or (key == "quantity" and not isinstance(value, int))):
                        raise ValueError(f"Invalid {key} for item {item_id}")


def fetch_batch_with_retry(dc, ids_param, attempts=4, *, stop_event=None, request_number=None):
    delay = 2
    for attempt in range(1, attempts + 1):
        if stop_event is not None and stop_event.is_set():
            raise CancelledError('Refresh stopped')
        resp = None
        try:
            pace_request()
            if stop_event is not None and stop_event.is_set():
                raise CancelledError('Refresh stopped')
            resp = request_market_data(dc, ids_param, attempt)
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get('Retry-After')
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            pass
            elif 400 <= resp.status_code < 500:
                print(f"Permanent HTTP {resp.status_code} for {dc}", file=sys.stderr)
                log_request_event({"event": "batch_rejected", "request_id": resp._debug_request_id,
                                   "status_code": resp.status_code, "will_retry": False})
                return None
            resp.raise_for_status()
            payload = resp.json()
            validate_market_payload(payload, set(ids_param.split(',')))
            log_request_event({"event": "response_validated", "request_id": resp._debug_request_id,
                               "unresolved_items": payload.get('unresolvedItems', [])})
            return payload
        except (requests.exceptions.RequestException, ValueError, TypeError) as e:
            retry_delay = delay + random.uniform(0, 0.5) if attempt < attempts else None
            log_request_event({"event": "attempt_failed", "request_id": getattr(resp, '_debug_request_id', None),
                               "data_center": dc, "item_ids": list(map(int, ids_param.split(','))),
                               "attempt": attempt, "error_type": type(e).__name__, "error": str(e),
                               "will_retry": attempt < attempts, "retry_delay_seconds": retry_delay})
            if attempt == attempts:
                print(f"  giving up on {dc} batch after {attempts} attempts: {e}")
                return None
            if retry_delay >= 120:
                print(f"  [{threading.current_thread().name}] Request {request_number or 'unassigned'} "
                      f"({dc}, attempt {attempt}/{attempts}) starting {retry_delay / 60:.2f}-minute "
                      f"retry wait ({retry_delay:.1f}s): {e}", flush=True)
            if stop_event is None:
                time.sleep(retry_delay)
            elif stop_event.wait(retry_delay):
                raise CancelledError('Refresh stopped during retry wait')
            delay = min(delay * 2, 60)
    return None


def fetch_dc_batch(dc, batch, *, stop_event=None, cache_path=None, request_number=None):
    """Returns {item_id_str: {"listings": [...], "history": [...]}} for one DC/batch."""
    ids_param = ",".join(str(x) for x in batch)
    payload = fetch_batch_with_retry(dc, ids_param, stop_event=stop_event, request_number=request_number)
    if payload is None:
        if len(batch) > 5:
            log_request_event({"event": "batch_split", "data_center": dc, "item_ids": batch})
            midpoint = len(batch) // 2
            left = fetch_dc_batch(dc, batch[:midpoint], stop_event=stop_event, cache_path=cache_path,
                                  request_number=f'{request_number}.1' if request_number is not None else None)
            right = fetch_dc_batch(dc, batch[midpoint:], stop_event=stop_event, cache_path=cache_path,
                                   request_number=f'{request_number}.2' if request_number is not None else None)
            if left is not None and right is not None:
                return {**left, **right}
        return None

    if len(batch) == 1:
        items = {str(batch[0]): payload}
    else:
        items = payload.get("items", {})

    out = {}
    for item_id_str, entry in items.items():
        listings = [
            (l["worldName"], l["hq"], l["pricePerUnit"], l["quantity"])
            for l in entry.get("listings", [])
        ]
        history = [
            (h["worldName"], h["hq"], h["pricePerUnit"], h["quantity"], h["timestamp"])
            for h in entry.get("recentHistory", [])
        ]
        records = entry.get("listings", []) + entry.get("recentHistory", [])
        names = {str(record["worldID"]): record["worldName"] for record in records if "worldID" in record}
        updates = entry.get("worldUploadTimes", {})
        updates = {names.get(str(world), str(world)): timestamp / 1000
                   for world, timestamp in updates.items()
                   if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and math.isfinite(timestamp) and timestamp > 0} if isinstance(updates, dict) else {}
        if not updates and isinstance(entry.get("lastUploadTime"), (int, float)) and not isinstance(entry["lastUploadTime"], bool) and math.isfinite(entry["lastUploadTime"]) and entry["lastUploadTime"] > 0:
            updates = {dc: entry["lastUploadTime"] / 1000}
        out[item_id_str] = {"listings": listings, "history": history, "source_updates": updates}
    if cache_path is not None:
        fetched_at = time.time()
        save_samples(cache_path, dc, batch, out, fetched_at, LISTING_ENTRIES, HISTORY_ENTRIES)
        for item in batch:
            key = str(item)
            out.setdefault(key, {"listings": [], "history": [], "source_updates": {}, "unresolved": True})
            out[key]['fetched_at'] = fetched_at
        log_request_event({"event": "batch_persisted", "data_center": dc, "item_ids": batch,
                           "fetched_at": fetched_at, "cache_path": str(cache_path)})
    return out


def price_stats(listings, history, *, snapshot_at=None, span_days=None):
    """Prices plus a sampled unit rate; callers may supply a shared DC window."""
    if snapshot_at is None:
        snapshot_at = time.time()
    if span_days is None and history:
        span_days = max((snapshot_at - min(h[4] for h in history)) / 86400, MIN_SPAN_DAYS)
    if span_days is not None:
        span_days = min(span_days, DEMAND_LOOKBACK_DAYS)
    # Preserve price references, but measure demand only in the recent window.
    # Apply the same cutoff to every world and quality sharing a DC window.
    demand_history = [h for h in history
                      if snapshot_at - DEMAND_LOOKBACK_DAYS * 86400 <= h[4] <= snapshot_at]
    nq_prices = [entry[2] for entry in listings if not entry[1]]
    any_prices = [entry[2] for entry in listings]
    min_nq = min(nq_prices) if nq_prices else None
    min_any = min(any_prices) if any_prices else None

    if history:
        # Median, not mean: real recentHistory is contaminated by RMT/gil-laundering
        # "sales" -- e.g. a Copper Ring "sold" for 999,998,999 gil to funnel gil between
        # accounts, alongside genuine sales in the low thousands. A quantity-weighted
        # mean lets a single such entry dominate; the median shrugs it off as long as
        # fake entries stay a minority of the sample, which they reliably are.
        avg_price = statistics.median(p for (_, _, p, _, _) in history)
        total_qty = sum(h[3] for h in demand_history)
        velocity = total_qty / span_days
    else:
        avg_price = None
        velocity = 0.0

    return {
        "last_sale_at": max((entry[4] for entry in history), default=None),
        "min_price_nq": min_nq,
        "min_price_any": min_any,
        "avg_price": round(avg_price, 1) if avg_price is not None else None,
        "sale_velocity_per_day": round(velocity, 3),
        "sales_sample": {
            "transactions": len(demand_history),
            "units": sum(h[3] for h in demand_history),
            "window_days": span_days,
            "snapshot_at": snapshot_at,
            "status": "no_history" if not demand_history else "sparse" if len(demand_history) < 5 else "sampled",
        },
    }


def aggregate_item(dc_samples, snapshot_at, *, split_quality=True, windows=None):
    """Keep independent history windows separate when estimating demand."""
    listings = [entry for sample in dc_samples.values() for entry in sample["listings"]]
    history = [entry for sample in dc_samples.values() for entry in sample["history"]]
    region = price_stats(listings, history, snapshot_at=snapshot_at)
    worlds = {}
    world_parents = {}
    data_centers = {}
    for dc, sample in dc_samples.items():
        sample_time = sample.get('fetched_at', snapshot_at)
        stats = price_stats(sample["listings"], sample["history"], snapshot_at=sample_time,
                            span_days=(windows or {}).get(dc))
        data_centers[dc] = stats
        window = stats["sales_sample"]["window_days"]
        by_world_listings = defaultdict(list)
        by_world_history = defaultdict(list)
        for entry in sample["listings"]:
            by_world_listings[entry[0]].append(entry)
        for entry in sample["history"]:
            by_world_history[entry[0]].append(entry)
        for world in set(by_world_listings) | set(by_world_history):
            world_parents[world] = dc
            worlds[world] = price_stats(
                by_world_listings[world], by_world_history[world],
                snapshot_at=sample_time, span_days=window,
            )
    # Sum unrounded rates; each DC has its own observation duration.
    region["sale_velocity_per_day"] = round(sum(
        stats["sales_sample"]["units"] / stats["sales_sample"]["window_days"]
        for stats in data_centers.values() if stats["sales_sample"]["window_days"]
    ), 3)
    region["sales_sample"] = {
        "snapshot_at": snapshot_at,
        "data_centers": {dc: stats["sales_sample"] for dc, stats in data_centers.items()},
    }
    updates = {world: timestamp for sample in dc_samples.values() for world, timestamp in sample.get("source_updates", {}).items()}
    region["source_updated_at"] = min(updates.values(), default=None)
    region["source_latest_at"] = max(updates.values(), default=None)
    region["source_time_incomplete"] = any(not sample.get("source_updates") for sample in dc_samples.values()) or any(entry[0] not in updates for entry in listings + history)
    for world, stats in worlds.items():
        stats["source_updated_at"] = updates.get(world)
        stats["source_time_incomplete"] = world not in updates
    result = {"region": region, "worlds": worlds}
    if split_quality:
        # Keep stacks intact: market-board purchases cannot split a listing.
        # None distinguishes old quantity-less snapshots from an empty market.
        region["purchase_listings"] = [
            {"world": entry[0], "quality": "hq" if entry[1] else "nq",
             "unit_price": entry[2], "quantity": entry[3],
             **({"source_updated_at": updates[entry[0]]} if entry[0] in updates else {})}
            for entry in listings
        ] if all(len(entry) == 4 for entry in listings) else None
        windows = {dc: stats["sales_sample"]["window_days"] for dc, stats in data_centers.items()}
        for quality, hq in (("nq", False), ("hq", True)):
            filtered = {dc: {
                "listings": [entry for entry in sample["listings"] if entry[1] == hq],
                "history": [entry for entry in sample["history"] if entry[1] == hq],
                "source_updates": sample.get("source_updates", {}),
                "fetched_at": sample.get('fetched_at', snapshot_at),
            } for dc, sample in dc_samples.items()}
            separated = aggregate_item(filtered, snapshot_at, split_quality=False, windows=windows)
            region.setdefault("qualities", {})[quality] = separated["region"]
            for world, stats in worlds.items():
                # Retain an empty quality slice even when that world has no matching records.
                parent = world_parents[world]
                stats.setdefault("qualities", {})[quality] = separated["worlds"].get(world) or price_stats(
                    [], [], snapshot_at=snapshot_at, span_days=windows[parent])
    return result


def main(*, max_age_hours=12, force=False):
    if not math.isfinite(max_age_hours) or max_age_hours < 0:
        raise ValueError('max_age_hours must be finite and nonnegative')
    print(f"Request debug log: {REQUEST_LOG_PATH}", flush=True)
    if not RECIPES_PATH.exists():
        sys.exit(f"Missing {RECIPES_PATH}; run fetch_recipes.py first")

    recipes = json.loads(RECIPES_PATH.read_text())
    if not isinstance(recipes, list) or not recipes:
        sys.exit("No recipes to refresh; existing data was preserved")
    item_ids = collect_item_ids(recipes)
    print(f"Fetching NA-region prices for {len(item_ids)} unique items across "
          f"{len(NA_DATA_CENTERS)} data centers ({sum(len(w) for w in NA_DATA_CENTERS.values())} worlds)...")

    cache_path = Path(os.environ.get('FFXIV_CACHE_PATH', OUT_PATH.parent / 'market_cache.sqlite3'))
    raw = defaultdict(dict, load_samples(cache_path, item_ids, NA_DATA_CENTERS, LISTING_ENTRIES, HISTORY_ENTRIES))
    started_at = time.time()
    cutoff = started_at - max_age_hours * 3600
    work_items = [(dc, batch) for dc in NA_DATA_CENTERS for batch in chunked(
        [item for item in item_ids if force or not cutoff <= raw.get(str(item), {}).get(dc, {}).get('fetched_at', 0) <= started_at], BATCH_SIZE)]
    total = len(work_items)
    cached_count = sum(1 for samples in raw.values() for sample in samples.values()
                       if cutoff <= sample.get('fetched_at', 0) <= started_at)
    print(f'{cached_count} fresh item/DC samples available in persistent cache; max age {max_age_hours:g} hours')
    log_request_event({'event': 'refresh_planned', 'batches': total, 'fresh_cached_samples': cached_count,
                       'max_age_hours': max_age_hours, 'force': force, 'cache_path': str(cache_path)})
    print(f"{total} batch requests queued ({MAX_WORKERS} concurrent workers)")
    write_progress(0, total)

    done = 0
    failed_batches = []
    stop_event = threading.Event()
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = {}
    remaining = iter(enumerate(work_items, start=1))
    try:
        while True:
            # Keep only one batch per worker submitted. If the collector fails,
            # there is no backlog of thousands of requests left running.
            while len(futures) < MAX_WORKERS:
                work = next(remaining, None)
                if work is None:
                    break
                request_number, (dc, batch) = work
                futures[pool.submit(fetch_dc_batch, dc, batch, stop_event=stop_event, cache_path=cache_path,
                                    request_number=request_number)] = (dc, batch)
            if not futures:
                break
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                dc, batch = futures.pop(future)
                try:
                    result = future.result()
                except (CancelledError, CacheWriteError):
                    raise
                except Exception as e:
                    print(f"Failed to fetch batch for {dc}: {e}", file=sys.stderr)
                    result = None

                if result is None:
                    failed_batches.append((dc, batch))
                else:
                    for item_id_str, data in result.items():
                        raw[item_id_str][dc] = data

                done += 1
                log_request_event({"event": "batch_collected", "data_center": dc, "item_ids": batch,
                                   "done": done, "total": total, "failed": len(failed_batches),
                                   "success": result is not None})
                write_progress(done, total, len(failed_batches))
                if done % 20 == 0 or done == total:
                    print(f"  {done}/{total} batches done", flush=True)
    except BaseException as exc:
        stop_event.set()
        for future in futures:
            future.cancel()
        print(f'Refresh aborted; cancelling outstanding work: {exc}', file=sys.stderr, flush=True)
        log_request_event({"event": "refresh_aborted", "error_type": type(exc).__name__,
                           "error": str(exc), "done": done, "total": total})
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    # Recover successful split batches even if a sibling failed or the collector
    # did not receive the parent result. Include old observations as fallback.
    for item, samples in load_samples(cache_path, item_ids, NA_DATA_CENTERS, LISTING_ENTRIES, HISTORY_ENTRIES).items():
        raw[item].update(samples)

    print(f"Computing region + per-world stats for {len(raw)} items...")
    snapshot_at = time.time()  # One reference time for every rate in this refresh.
    try:
        previous = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    wanted_ids = set(map(str, item_ids))
    prices = {}
    incompatible_ids = set()
    for item, value in previous.items():
        if item in wanted_ids:
            if compatible_price(value):
                prices[item] = value
            else:
                incompatible_ids.add(item)
    incompatible = len(incompatible_ids)
    retained_ids = set(prices)
    updated = 0
    for item_id, samples in raw.items():
        if set(NA_DATA_CENTERS) <= set(samples):
            prices[item_id] = aggregate_item(samples, snapshot_at)
            prices[item_id]['fetched_at_by_dc'] = {dc: sample.get('fetched_at') for dc, sample in samples.items()}
            retained_ids.discard(item_id)
            updated += 1

    if not prices:
        sys.exit("No compatible item estimates can be built yet. Complete observations for all data centers "
                 "are required for the initial migration. Successful requests remain cached; existing data was preserved.")
    if (not previous and not any(
            sample['listings'] or sample['history'] for samples in raw.values() for sample in samples.values())):
        sys.exit("Refresh returned no usable market observations; existing data was preserved")
    failed_pairs = {(str(item), dc) for dc, batch in failed_batches for item in batch}
    for item_id, entry in prices.items():
        fetched = entry.get('fetched_at_by_dc', {})
        entry['refresh_coverage'] = {
            'assessed_at': snapshot_at, 'max_age_hours': max_age_hours,
            'required_data_centers': list(NA_DATA_CENTERS),
            'fetched_at_by_dc': fetched,
            'missing_data_centers': [dc for dc in NA_DATA_CENTERS if dc not in raw.get(item_id, {})],
            'stale_data_centers': [dc for dc in NA_DATA_CENTERS if fetched.get(dc) is not None and fetched[dc] < cutoff],
            'failed_data_centers': [dc for dc in NA_DATA_CENTERS if (item_id, dc) in failed_pairs and (fetched.get(dc) or 0) < started_at],
            'observation_time_unknown': any(fetched.get(dc) is None for dc in NA_DATA_CENTERS),
            'retained_previous': item_id in retained_ids,
        }
    stale = sum(sample.get('fetched_at', 0) < cutoff for samples in raw.values() for sample in samples.values())
    missing = sum(dc not in raw.get(str(item), {}) for item in item_ids for dc in NA_DATA_CENTERS)
    omitted_legacy = len(incompatible_ids - set(prices))
    warning = (f'Partial refresh: {len(failed_batches)} failed batches; {stale} stale item/DC samples retained; '
               f'{missing} missing item/DC samples; {omitted_legacy} incompatible previous entries omitted. '
               f'Successful requests are saved for the next refresh.'
               if failed_batches or stale or missing or omitted_legacy else None)
    atomic_json(OUT_PATH.parent / 'refresh_summary.json', {'failed_batches': len(failed_batches),
                'stale_samples': stale, 'missing_samples': missing, 'updated_items': updated,
                'incompatible_previous_entries': incompatible, 'omitted_legacy_items': omitted_legacy, 'retained_items': len(retained_ids),
                'assessed_at': snapshot_at, 'max_age_hours': max_age_hours, 'warning': warning})
    if warning:
        print(warning, file=sys.stderr, flush=True)
    atomic_json(OUT_PATH, prices)
    log_request_event({"event": "prices_saved", "items": len(prices), "path": str(OUT_PATH)})
    print(f"Saved prices for {len(prices)} items to {OUT_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Refresh missing/stale market samples; keep successful requests across runs.')
    parser.add_argument('--max-age-hours', type=float, default=12, help='Reuse samples fetched within this many hours (default: 12).')
    parser.add_argument('--force', action='store_true', help='Request every item/DC again, retaining old data if requests fail.')
    options = parser.parse_args()
    main(max_age_hours=options.max_age_hours, force=options.force)
