"""Validate a complete static export before uploading it to GitHub Pages."""
import argparse
import json
import math
from pathlib import Path


def validate(site):
    site = Path(site).resolve()

    def read(relative):
        path = (site / relative).resolve()
        if not path.is_relative_to(site):
            raise ValueError('Snapshot path escapes the published site')
        return json.loads(path.read_text(encoding='utf-8'))

    def amount(value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value < 0):
            raise ValueError('Invalid price or demand in static export')

    def messages(value):
        if not isinstance(value, list) or any(not isinstance(message, str) for message in value):
            raise ValueError('Invalid warning messages')

    html = (site / 'index.html').read_text(encoding='utf-8')
    if '{{' in html or 'id="refresh-btn"' in html:
        raise ValueError('Export contains unrendered template or public refresh controls')
    for asset in ('static/app.js', 'static/static-data.js', 'static/style.css', '.nojekyll'):
        if not (site / asset).is_file():
            raise ValueError(f'Missing website asset: {asset}')
    manifest = read('data/manifest.json')
    if manifest.get('schema') != 1 or 'region' not in manifest.get('scopes', {}):
        raise ValueError('Missing or unsupported static manifest')
    amount(manifest.get('updated_at'))
    recipes = read(manifest['recipes'])
    if not isinstance(recipes, list) or not recipes:
        raise ValueError('Export has no recipes')
    group = {}
    for index, recipe in enumerate(recipes):
        if index % 100 == 0:
            group = read(f"{manifest['ingredients']}{index // 100}.json")
        details = group[str(index)]
        for mode, (cost, warnings) in recipe['costs'].items():
            amount(cost)
            messages(warnings)
            for ingredient in details[mode]:
                if not isinstance(ingredient['name'], str):
                    raise ValueError('Invalid ingredient name')
                amount(ingredient['quantity'])
                amount(ingredient['subtotal'])
    usable = False
    for relative in manifest['scopes'].values():
        scope = read(relative)
        for index, choices in scope.items():
            if not index.isdigit() or not 0 <= int(index) < len(recipes):
                raise ValueError('Selling estimate refers to a missing recipe')
            for quality, value in choices.items():
                if isinstance(value, str):
                    if quality != 'auto' or value not in ('nq', 'hq') or not isinstance(choices.get(value), list):
                        raise ValueError('Invalid Auto quality reference')
                    continue
                revenue, velocity, product_quality, warnings = value
                amount(revenue)
                amount(velocity)
                messages(warnings)
                usable |= revenue is not None and any(cost[0] is not None for cost in recipes[int(index)]['costs'].values())
    if not usable:
        raise ValueError('Export contains no usable crafting estimates')
    size = sum(path.stat().st_size for path in site.rglob('*') if path.is_file())
    if size >= 900_000_000:
        raise ValueError('Export exceeds the project size budget')
    return dict(recipes=len(recipes), scopes=len(manifest['scopes']), bytes=size)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, default=Path('site'))
    print(json.dumps(validate(parser.parse_args().site), indent=2))
