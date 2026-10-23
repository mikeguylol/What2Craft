"""Validate table queries and paginate only after filtering and sorting."""
import math

SORT_KEYS = {'item', 'profit', 'cost', 'margin_pct', 'sale_velocity_per_day',
             'daily_profit_potential', 'revenue', 'crafter_job', 'level', 'yield_qty'}


def table_query(args):
    def choice(key, default, choices):
        value = args.get(key, default)
        if value not in choices:
            raise ValueError(f'Invalid {key}')
        return value

    def integer(key, default, maximum=None):
        try:
            value = int(args.get(key, default))
        except (TypeError, ValueError):
            raise ValueError(f'Invalid {key}') from None
        if value < 1 or (maximum is not None and value > maximum):
            raise ValueError(f'Invalid {key}')
        return value

    query = dict(page=integer('page', 1), page_size=integer('page_size', 100, 500),
                 search=args.get('search', '').strip().casefold(),
                 job=choice('job', '', {'', 'CRP', 'BSM', 'ARM', 'GSM', 'LTW', 'WVR', 'ALC', 'CUL'}),
                 certainty=choice('certainty', 'all', {'all', 'available', 'current', 'stronger'}),
                 demand=choice('demand', 'all', {'all', 'observed', 'zero', 'unknown'}),
                 sort_key=choice('sort_key', 'profit', SORT_KEYS),
                 sort_dir=choice('sort_dir', 'desc', {'asc', 'desc'}))
    for name in ('max_level', 'max_cost', 'min_profit', 'min_demand'):
        value = args.get(name)
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(f'Invalid {name}') from None
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'Invalid {name}')
        query[name] = value
    return query


def paginate(rows, query):
    def matches(row):
        if query['search'] and query['search'] not in row['item'].casefold():
            return False
        if query['job'] and row['crafter_job'] != query['job']:
            return False
        for limit, field, minimum in (('max_level', 'level', False), ('max_cost', 'cost', False),
                                      ('min_profit', 'profit', True), ('min_demand', 'sale_velocity_per_day', True)):
            value, bound = row.get(field), query[limit]
            if bound is not None and (value is None or (value < bound if minimum else value > bound)):
                return False
        certainty = query['certainty']
        if certainty != 'all' and row['profit'] is None:
            return False
        if certainty == 'current' and (row['cost_historical'] or row['price_source'] != 'current'):
            return False
        if certainty == 'stronger' and row['uncertain']:
            return False
        demand = query['demand']
        if demand == 'observed' and row['sales_transactions'] <= 0:
            return False
        if demand == 'zero' and row['demand_status'] != 'No observed sales':
            return False
        if demand == 'unknown' and row['demand_status'] != 'Unknown demand':
            return False
        return True

    filtered = [row for row in rows if matches(row)]
    key = query['sort_key']
    known = [row for row in filtered if row.get(key) is not None]
    unknown = [row for row in filtered if row.get(key) is None]
    known.sort(key=lambda row: row[key].casefold() if isinstance(row[key], str) else row[key],
               reverse=query['sort_dir'] == 'desc')
    ordered = known + unknown
    count = len(ordered)
    pages = max(1, math.ceil(count / query['page_size']))
    page = min(query['page'], pages)
    start = (page - 1) * query['page_size']
    return ordered[start:start + query['page_size']], dict(
        count=count, page=page, page_size=query['page_size'], total_pages=pages, dataset_count=len(rows))
