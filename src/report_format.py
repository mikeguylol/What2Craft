"""Compact report storage and selection without expanding the whole snapshot."""


def validate_quality_references(scope):
    options = scope.get('quality_options', {})
    if not isinstance(options, dict):
        raise ValueError('Malformed quality options')
    for name, option in options.items():
        if isinstance(option, str):
            if name != 'auto' or option not in ('nq', 'hq') or not isinstance(options.get(option), dict):
                raise ValueError('Malformed Auto quality reference')
        elif not isinstance(option, dict):
            raise ValueError('Malformed quality option')


def quality_option(scope, quality='auto'):
    options = scope.get('quality_options')
    if options is None:
        return scope if quality == 'auto' else {}
    option = options.get(quality, {})
    # Only Auto can reference a forced quality. No chains or cross-quality fallback.
    if quality == 'auto' and isinstance(option, str) and option in ('nq', 'hq'):
        option = options.get(option, {})
    return option if isinstance(option, dict) else {}


def cost_option(row, quality='cheapest'):
    if 'cost_options' in row:
        return row['cost_options'].get(quality, {})
    return row if quality == 'cheapest' else {}


def compact_row(row):
    """Keep explicit pooled Auto estimates; alias only identical forced options."""
    result = {key: value for key, value in row.items() if key not in ('cost', 'ingredients', 'scopes')}
    result['report_schema'] = 2
    result['scopes'] = {}
    for name, scope in row['scopes'].items():
        choices = dict(scope['quality_options'])
        auto = choices.get('auto')
        for quality in ('nq', 'hq'):
            if isinstance(auto, dict) and quality in choices and auto == choices[quality]:
                choices['auto'] = quality
                break
        result['scopes'][name] = {'quality_options': choices}
    return result
