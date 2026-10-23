"""Compatibility checks for retained prices and durable raw observations."""
import math
import time
from ffxiv_data import NA_DATA_CENTERS


def finite_number(value, minimum=0):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= minimum


def valid_sample(sample, fetched_at):
    if not isinstance(sample, dict) or not finite_number(fetched_at, 1):
        return False
    updates = sample.get('source_updates')
    if not isinstance(updates, dict) or any(not isinstance(world, str) or not finite_number(stamp, 1) for world, stamp in updates.items()):
        return False
    for key, size in (('listings', 4), ('history', 5)):
        records = sample.get(key)
        if not isinstance(records, list):
            return False
        for record in records:
            if not isinstance(record, (list, tuple)) or len(record) != size:
                return False
            world, hq, price, quantity = record[:4]
            if not isinstance(world, str) or not world or not isinstance(hq, bool) or not finite_number(price):
                return False
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                return False
            if size == 5 and not finite_number(record[4], 1):
                return False
    if 'unresolved' in sample and not isinstance(sample['unresolved'], bool):
        return False
    return not sample.get('unresolved') or not (sample['listings'] or sample['history'])


def valid_sales_sample(sample):
    if not isinstance(sample, dict):
        return False
    if 'data_centers' in sample:
        return isinstance(sample['data_centers'], dict) and all(valid_sales_sample(s) for s in sample['data_centers'].values())
    return (finite_number(sample.get('transactions')) and finite_number(sample.get('units'))
            and (sample.get('window_days') is None or finite_number(sample['window_days'])))


def valid_stats(stats):
    if not isinstance(stats, dict) or not all(key in stats for key in ('min_price_any', 'min_price_nq', 'avg_price', 'sale_velocity_per_day', 'sales_sample')):
        return False
    if any(stats[key] is not None and not finite_number(stats[key]) for key in ('min_price_any', 'min_price_nq', 'avg_price')):
        return False
    if not finite_number(stats['sale_velocity_per_day']) or not valid_sales_sample(stats['sales_sample']):
        return False
    for key in ('source_updated_at', 'source_latest_at', 'last_sale_at'):
        if stats.get(key) is not None and not finite_number(stats[key], 1):
            return False
    return 'source_time_incomplete' not in stats or isinstance(stats['source_time_incomplete'], bool)


def compatible_price(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get('region'), dict) or not isinstance(entry.get('worlds'), dict):
        return False
    listings = entry['region'].get('purchase_listings')
    if not isinstance(listings, list):
        return False
    for listing in listings:
        if (not isinstance(listing, dict) or not isinstance(listing.get('world'), str)
                or listing.get('quality') not in ('nq', 'hq') or not finite_number(listing.get('unit_price'))
                or not isinstance(listing.get('quantity'), int) or isinstance(listing['quantity'], bool) or listing['quantity'] <= 0):
            return False
        if listing.get('source_updated_at') is not None and not finite_number(listing['source_updated_at'], 1):
            return False
    for scope in [entry['region'], *entry['worlds'].values()]:
        if not valid_stats(scope) or not isinstance(scope.get('qualities'), dict):
            return False
        if not all(valid_stats(scope['qualities'].get(q)) for q in ('nq', 'hq')):
            return False
    fetched = entry.get('fetched_at_by_dc', {})
    if not isinstance(fetched, dict) or any(stamp is not None and not finite_number(stamp, 1) for stamp in fetched.values()):
        return False
    return True


def valid_coverage(coverage):
    if not isinstance(coverage, dict) or not isinstance(coverage.get('fetched_at_by_dc', {}), dict):
        return False
    if not finite_number(coverage.get('max_age_hours', 12)):
        return False
    if any(stamp is not None and not finite_number(stamp, 1) for stamp in coverage.get('fetched_at_by_dc', {}).values()):
        return False
    for key in ('required_data_centers', 'missing_data_centers', 'stale_data_centers', 'failed_data_centers'):
        if not isinstance(coverage.get(key, []), list) or any(not isinstance(dc, str) for dc in coverage.get(key, [])):
            return False
    return all(key not in coverage or isinstance(coverage[key], bool) for key in ('retained_previous', 'observation_time_unknown'))


def coverage_metadata(entry, max_age_hours=12):
    """Also support completed prices written before coverage summaries existed."""
    if isinstance(entry.get('refresh_coverage'), dict) and valid_coverage(entry['refresh_coverage']):
        return entry['refresh_coverage']
    fetched = entry.get('fetched_at_by_dc', {})
    fetched = fetched if isinstance(fetched, dict) else {}
    now = time.time()
    return dict(assessed_at=now, max_age_hours=max_age_hours,
                required_data_centers=list(NA_DATA_CENTERS), fetched_at_by_dc=fetched,
                missing_data_centers=[dc for dc in NA_DATA_CENTERS if dc not in fetched],
                stale_data_centers=[dc for dc, stamp in fetched.items() if finite_number(stamp, 1) and stamp < now - max_age_hours * 3600],
                failed_data_centers=[], observation_time_unknown=any(fetched.get(dc) is None for dc in NA_DATA_CENTERS),
                retained_previous=False)
