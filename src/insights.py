"""Explain uncertainty and opportunity without hiding the underlying estimates."""
STALE_AFTER_SECONDS = 24 * 60 * 60
WARNING_MESSAGES = {
    'Estimate unavailable': 'Insufficient price data',
    'Unknown demand': 'Sales data unavailable',
    'No observed sales': 'No observed sales',
    'Sparse sales history': 'Very few recorded sales',
    'Historical cost': 'Ingredient price based on past sales',
    'Limited observed supply': 'Insufficient listed ingredients; cost extrapolated',
    'Historical revenue': 'Selling price based on past sales',
    'Price differs from recent sales': 'Listing price differs greatly from recorded sales',
    'Incomplete refresh coverage': 'Some data centers are missing from the estimate',
}


def describe_estimate(row, now):
    sample = row.get('sales_sample')
    samples = list(sample.get('data_centers', {}).values()) if sample and 'data_centers' in sample else [sample] if sample else []
    transactions = sum(s.get('transactions', 0) for s in samples)
    velocity = row.get('sale_velocity_per_day')
    if velocity is None or not samples:
        demand = 'Unknown demand'
    elif transactions == 0:
        demand = 'No observed sales'
    elif velocity < 0.5:
        demand = 'Low observed demand'
    else:
        demand = 'Observed sales'
    flags = []
    if row.get('unavailable_reason'):
        flags.append('Estimate unavailable')
    if row.get('cost_historical'):
        flags.append('Historical cost')
    if row.get('cost_limited_supply'):
        flags.append('Limited observed supply')
    if row.get('price_source') == 'historical':
        flags.append('Historical revenue')
    if row.get('price_difference'):
        flags.append('Price differs from recent sales')
    if not samples or transactions < 5:
        flags.append('Sparse sales history' if transactions else demand)
    coverages = [row.get('market_coverage', {})] + [i.get('market_coverage', {}) for i in row.get('ingredients', [])]
    for coverage in coverages:
        fetched = coverage.get('fetched_at_by_dc', {})
        cutoff = now - coverage.get('max_age_hours', 12) * 3600
        if coverage.get('retained_previous'):
            flags.append('Previous item estimate retained')
        if coverage.get('stale_data_centers') or any(stamp is not None and stamp < cutoff for stamp in fetched.values()):
            flags.append('Stale cached observations')
        if coverage.get('missing_data_centers') or coverage.get('failed_data_centers'):
            flags.append('Incomplete refresh coverage')
        if coverage and coverage.get('observation_time_unknown'):
            flags.append('Observation time unknown')
    times = [row.get('source_updated_at')] + [i.get('source_updated_at') for i in row.get('ingredients', [])]
    known = [t for t in times if t is not None]
    oldest = min(known, default=None)
    incomplete = row.get('source_time_incomplete', True) or any(t is None for t in times)
    if oldest is not None and now - oldest > STALE_AFTER_SECONDS:
        flags.append('Source data older than 24h')
    if incomplete:
        flags.append('Source age partly unknown' if known else 'Source age unknown')
    warnings = list(dict.fromkeys(WARNING_MESSAGES[flag] for flag in flags if flag in WARNING_MESSAGES))
    return dict(estimate_flags=list(dict.fromkeys(flags)), data_warnings=warnings, uncertain=bool(warnings),
                demand_status=demand, sales_transactions=transactions,
                oldest_source_at=oldest, source_time_incomplete=incomplete)
