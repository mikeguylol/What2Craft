"""Quality selection rules shared by report generation and the web API."""
from market_validation import coverage_metadata
POOL_PRICE_TOLERANCE = 0.10
POOL_MIN_TRANSACTIONS = 5
INGREDIENT_SAMPLE_UNITS = 5
COST_SCHEMA = 3
PRICE_DIFFERENCE_MULTIPLIER = 3


def sample_count(stats):
    sample = stats.get('sales_sample') or {}
    if 'data_centers' in sample:
        return sum(s.get('transactions', 0) for s in sample['data_centers'].values())
    return sample.get('transactions', 0)


def sale_price(stats):
    if not stats:
        return None
    listing = stats.get('min_price_any')
    median = stats.get('avg_price')
    if listing is None:
        return median
    return listing


def price_details(stats):
    listing, median = stats.get('min_price_any'), stats.get('avg_price')
    differs = (listing is not None and median is not None and
               max(listing, median) > PRICE_DIFFERENCE_MULTIPLIER * min(listing, median))
    return dict(current_unit_price=listing, historical_unit_price=median,
                price_source='current' if listing is not None else 'historical',
                price_difference=differs, source_updated_at=stats.get('source_updated_at'),
                source_time_incomplete=stats.get('source_time_incomplete', True), last_sale_at=stats.get('last_sale_at'))


def product_options(stats, can_be_hq):
    """Forced modes never cross qualities. Auto pools only with evidence."""
    qualities = stats.get('qualities', {})
    options = {}
    for quality in ('nq', 'hq'):
        qstats = qualities.get(quality)
        price = sale_price(qstats)
        if price is not None and not (quality == 'hq' and can_be_hq is False):
            options[quality] = dict(qstats, unit_price=price, product_quality=quality, **price_details(qstats))
    if not options:
        return options
    dominant = max(options, key=lambda q: options[q]['sale_velocity_per_day'])
    options['auto'] = options[dominant]
    if 'nq' in options and 'hq' in options:
        nq, hq = qualities['nq'], qualities['hq']
        a, b = nq.get('avg_price'), hq.get('avg_price')
        if (a and b and abs(a - b) / min(a, b) <= POOL_PRICE_TOLERANCE
                and min(sample_count(nq), sample_count(hq)) >= POOL_MIN_TRANSACTIONS):
            pooled = dict(stats, min_price_any=min(
                (s['min_price_any'] for s in (nq, hq) if s.get('min_price_any') is not None), default=None))
            options['auto'] = dict(pooled, unit_price=sale_price(pooled), product_quality='combined', **price_details(pooled))
    return options


def ingredient_estimate(listings, required, allowed, qualities=None):
    """Estimate consumed cost from a small quantity-weighted cheap-price sample.

    Use up to max(five units, one craft's requirement). Listings contribute
    only the units needed for that sample; leftover stack units are not charged.
    """
    if listings is None or required <= 0:
        return None
    candidates = []
    for listing in listings:
        quantity, price = listing.get('quantity'), listing.get('unit_price')
        if (listing.get('quality') not in allowed or not isinstance(quantity, int)
                or isinstance(quantity, bool) or quantity <= 0
                or not isinstance(price, (int, float)) or isinstance(price, bool) or price < 0):
            continue
        candidates.append(listing)
    remaining = max(INGREDIENT_SAMPLE_UNITS, required)
    sample = []
    for listing in sorted(candidates, key=lambda item: item['unit_price']):
        taken = min(remaining, listing['quantity'])
        sample.append(dict(listing, quantity=taken))
        remaining -= taken
        if remaining == 0:
            break
    sampled = sum(item['quantity'] for item in sample)
    observed = sum(item['quantity'] for item in candidates)
    if sampled == 0:
        history = [(stats['avg_price'], quality, stats) for quality, stats in (qualities or {}).items()
                   if quality in allowed and stats.get('avg_price') is not None and sample_count(stats) > 0]
        if not history:
            return None
        price, quality, stats = min(history, key=lambda entry: entry[0])
        return dict(subtotal=round(price * required, 1), unit_price=price,
                    sample_quantity=0, sample_listings=[], observed_quantity=0,
                    quality=quality, price_source='historical', limited_supply=True,
                    historical_transactions=sample_count(stats), source_updated_at=stats.get('source_updated_at'),
                    last_sale_at=stats.get('last_sale_at'))
    unit_price = sum(item['quantity'] * item['unit_price'] for item in sample) / sampled
    qualities = {item['quality'] for item in sample}
    return dict(subtotal=round(unit_price * required, 1), unit_price=unit_price,
                sample_quantity=sampled, sample_listings=sample, observed_quantity=observed,
                price_source='current', limited_supply=observed < required,
                source_updated_at=min((i['source_updated_at'] for i in sample), default=None) if all(i.get('source_updated_at') for i in sample) else None,
                quality=next(iter(qualities)) if len(qualities) == 1 else 'mixed')


class IngredientEstimates:
    """Reuse estimates within one immutable market snapshot, never across builds."""

    def __init__(self, prices):
        self.prices = prices
        self.estimates = {}
        self.coverage = {}

    def get(self, item_id, quantity, allowed):
        key = (str(item_id), quantity, allowed)
        if key not in self.estimates:
            region = self.prices.get(str(item_id), {}).get('region', {})
            self.estimates[key] = ingredient_estimate(
                region.get('purchase_listings'), quantity, allowed, region.get('qualities', {}))
        return self.estimates[key]

    def get_coverage(self, item_id):
        key = str(item_id)
        if key not in self.coverage:
            self.coverage[key] = coverage_metadata(self.prices.get(key, {}))
        return self.coverage[key]


def ingredient_options(recipe, prices, *, estimates=None):
    if estimates is None:
        estimates = IngredientEstimates(prices)
    elif estimates.prices is not prices:
        raise ValueError('Ingredient estimates must use the same market snapshot')
    options = {}
    # Merge repeated ingredients so two recipe slots cannot reuse one listing.
    required_items = {}
    for ing in recipe['ingredients']:
        item_id = str(ing['item_id'])
        if item_id in required_items:
            required_items[item_id]['quantity'] += ing['quantity']
        else:
            required_items[item_id] = dict(ing)
    for mode in ('cheapest', 'nq', 'hq'):
        ingredients = []
        for ing in required_items.values():
            if mode == 'hq':
                if ing.get('can_be_hq') is None:
                    break
                allowed = ('hq',) if ing['can_be_hq'] else ('nq',)
            else:
                allowed = ('nq', 'hq') if mode == 'cheapest' else ('nq',)
            estimate = estimates.get(ing['item_id'], ing['quantity'], allowed)
            if estimate is None:
                break
            ingredients.append(dict(ing, **estimate, market_coverage=estimates.get_coverage(ing['item_id'])))
        else:
            options[mode] = dict(cost=round(sum(i['subtotal'] for i in ingredients), 1), ingredients=ingredients)
    return options
