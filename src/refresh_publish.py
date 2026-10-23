"""Publish a completed refresh; the browser dataset is replaced last."""
import json
import math
import shutil
from file_io import replace_with_retry
from report_format import cost_option, quality_option, validate_quality_references

FILES = ('recipes.json', 'prices.json', 'profit_report.csv', 'profit_report_liquid.csv', 'refresh_summary.json', 'profit_data.json')


def validate_refresh(folder):
    for name in FILES:
        path = folder / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Refresh did not produce {name}')
    summary = json.loads((folder / 'refresh_summary.json').read_text())
    if not isinstance(summary, dict) or (summary.get('warning') is not None and not isinstance(summary['warning'], str)):
        raise ValueError('Refresh produced invalid refresh summary')
    for key in ('failed_batches', 'stale_samples', 'missing_samples', 'updated_items'):
        if key in summary and (not isinstance(summary[key], int) or isinstance(summary[key], bool) or summary[key] < 0):
            raise ValueError('Refresh produced invalid summary counts')
    recipes = json.loads((folder / 'recipes.json').read_text())
    prices = json.loads((folder / 'prices.json').read_text())
    rows = json.loads((folder / 'profit_data.json').read_text())
    if not isinstance(recipes, list) or not recipes or not isinstance(prices, dict) or not prices:
        raise ValueError('Refresh produced empty or invalid source data')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Refresh produced no crafting rows')
    usable = False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('scopes'), dict):
            raise ValueError('Refresh produced malformed crafting rows')
        cost = cost_option(row).get('cost')
        for scope in row['scopes'].values():
            if not isinstance(scope, dict):
                raise ValueError('Refresh produced malformed scope data')
            validate_quality_references(scope)
            scope = quality_option(scope)
            revenue = scope.get('revenue')
            for number in (cost, revenue, scope.get('sale_velocity_per_day')):
                if number is not None and (not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(number) or number < 0):
                    raise ValueError('Refresh produced invalid financial or sales values')
            usable |= cost is not None and revenue is not None
    if not usable:
        raise ValueError('Refresh produced no usable crafting estimates')


def publish_refresh(staging, destination):
    validate_refresh(staging)
    backup = staging / 'previous'
    backup.mkdir()
    # Finish backups before touching live files. Roll back replacements if a
    # later operation fails (e.g. a CSV is locked by another application).
    existed = set()
    for name in FILES:
        if (destination / name).exists():
            shutil.copy2(destination / name, backup / name)
            existed.add(name)
    replaced = []
    try:
        for name in FILES:
            replace_with_retry(staging / name, destination / name, preserve_hint=False)
            replaced.append(name)
    except OSError:
        for name in reversed(replaced):
            if name in existed:
                replace_with_retry(backup / name, destination / name, preserve_hint=False)
            else:
                (destination / name).unlink(missing_ok=True)
        raise
