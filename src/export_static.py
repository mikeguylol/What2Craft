"""Build a GitHub Pages preview from an existing report, without network requests."""
import argparse
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from ffxiv_data import NA_DATA_CENTERS
from insights import describe_estimate
from quality import COST_SCHEMA
from report_format import validate_quality_references

ROOT = Path(__file__).resolve().parents[1]


def report_rows(path):
    """Stream the top-level array so a 400 MB report needn't fit in memory."""
    decoder = json.JSONDecoder()
    with path.open(encoding='utf-8') as stream:
        buffer, eof = '', False
        def more():
            nonlocal buffer, eof
            chunk = stream.read(1024 * 1024)
            buffer += chunk
            eof = not chunk
        more()
        buffer = buffer.lstrip()
        if not buffer.startswith('['):
            raise ValueError('Expected a report array')
        buffer = buffer[1:]
        first = True
        while True:
            buffer = buffer.lstrip()
            while not buffer and not eof:
                more()
                buffer = buffer.lstrip()
            if buffer.startswith(']'):
                buffer = buffer[1:]
                if buffer.strip() or stream.read().strip():
                    raise ValueError('Unexpected content after report')
                return
            if not first:
                if not buffer.startswith(','):
                    raise ValueError('Expected a report separator')
                buffer = buffer[1:].lstrip()
            while True:
                try:
                    row, end = decoder.raw_decode(buffer)
                    break
                except ValueError:
                    if eof:
                        raise ValueError('Incomplete report') from None
                    more()
            buffer = buffer[end:]
            first = False
            yield row


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, separators=(',', ':'), allow_nan=False), encoding='utf-8')


def icon(path):
    return f'https://v2.xivapi.com/api/asset?path={path}&format=png' if path else None


def build(report, output):
    report, output = Path(report).resolve(), Path(output).resolve()
    protected = [ROOT / name for name in ('src', 'data', '.git')]
    if output in (ROOT, ROOT / 'tests') or output in report.parents or any(output == path or path in output.parents for path in protected):
        raise ValueError('Output must be a separate generated-site directory')
    if output.exists() and not (output / '.what2craft-static').is_file():
        raise ValueError('Refusing to replace a directory not created by this exporter')
    output.parent.mkdir(parents=True, exist_ok=True)
    build_id = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix='.what2craft-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'site'
        data = stage / 'data' / build_id
        recipes, scopes, details, warnings = [], {}, {}, set()
        for row in report_rows(report):
            if row.get('quality_schema') != 1 or row.get('cost_schema') != COST_SCHEMA:
                raise ValueError('Rebuild the report with current pricing rules before exporting')
            index = len(recipes)
            recipe = {key: row[key] for key in ('item', 'item_id', 'crafter_job', 'level', 'yield_qty')}
            recipe.update(icon_url=icon(row.get('icon_path')), can_be_hq=row.get('can_be_hq'), costs={})
            detail = {}
            for mode, cost in row.get('cost_options', {}).items():
                ingredients = cost.get('ingredients', [])
                flags = describe_estimate(dict(sales_sample={'transactions': 10}, sale_velocity_per_day=1,
                    cost_historical=any(i.get('price_source') == 'historical' for i in ingredients),
                    cost_limited_supply=any(i.get('price_source') == 'current' and i.get('limited_supply') for i in ingredients),
                    ingredients=ingredients), 0)['data_warnings']
                recipe['costs'][mode] = [cost.get('cost'), flags]
                detail[mode] = [dict(name=i['name'], quantity=i['quantity'], quality=i.get('quality'),
                                     subtotal=i['subtotal'], icon_url=icon(i.get('icon_path'))) for i in ingredients]
            recipes.append(recipe)
            details[str(index)] = detail
            if len(details) == 100:
                write_json(data / 'ingredients' / f'{index // 100}.json', details)
                details = {}
            for name, scope in row['scopes'].items():
                validate_quality_references(scope)
                target = scopes.setdefault(name, {})
                options = {}
                for quality, value in scope.get('quality_options', {}).items():
                    if isinstance(value, str):
                        options[quality] = value
                    else:
                        flags = describe_estimate(dict(value, market_coverage=row.get('market_coverage', {})), 0)['data_warnings']
                        options[quality] = [value.get('revenue'), value.get('sale_velocity_per_day'), value.get('product_quality'), flags]
                target[str(index)] = options
            if row.get('refresh_warning'):
                warnings.add(row['refresh_warning'])
        if not recipes:
            raise ValueError('Report has no recipes')
        if details:
            write_json(data / 'ingredients' / f'{(len(recipes) - 1) // 100}.json', details)
        write_json(data / 'recipes.json', recipes)
        scope_paths = {}
        for number, (name, values) in enumerate(scopes.items()):
            filename = f'data/{build_id}/scopes/{number}.json'
            write_json(stage / filename, values)
            scope_paths[name] = filename
        write_json(stage / 'data' / 'manifest.json', dict(schema=1, updated_at=report.stat().st_mtime,
            recipes=f'data/{build_id}/recipes.json', scopes=scope_paths,
            ingredients=f'data/{build_id}/ingredients/', data_centers=NA_DATA_CENTERS,
            warning=' '.join(sorted(warnings)) or None))
        shutil.copytree(ROOT / 'src' / 'static', stage / 'static')
        # Render the existing template once; the published site doesn't run Flask.
        from jinja2 import Environment
        template = (ROOT / 'src' / 'templates' / 'index.html').read_text(encoding='utf-8')
        html = Environment(autoescape=True).from_string(template).render(
            url_for=lambda endpoint, filename: f'./static/{filename}')
        import re
        html = re.sub(r'\s*<button id="refresh-btn".*?</button>', '', html)
        html = re.sub(r'\s*<span id="refresh-status".*?</span>', '', html)
        start = html.index('    <div id="refresh-progress-wrap"')
        end = html.index('    <p id="meta-line"', start)
        html = html[:start] + html[end:]
        html = html.replace('<script src="./static/app.js">', '<script src="./static/static-data.js"></script>\n  <script src="./static/app.js">')
        (stage / 'index.html').write_text(html, encoding='utf-8')
        (stage / '.nojekyll').touch()
        (stage / '.what2craft-static').touch()
        size = sum(p.stat().st_size for p in stage.rglob('*') if p.is_file())
        if size >= 900_000_000:
            raise ValueError('Static output is too large; existing preview was preserved')
        previous = Path(temporary) / 'previous'
        if output.exists():
            output.replace(previous)
        try:
            stage.replace(output)
        except OSError:
            if previous.exists():
                previous.replace(output)
            raise
    return dict(recipes=len(recipes), scopes=len(scopes), bytes=size, output=str(output))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=ROOT / 'data' / 'profit_data.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'site')
    args = parser.parse_args()
    print(json.dumps(build(args.report, args.output), indent=2))
