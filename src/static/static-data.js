/* Static snapshot adapter. All paths resolve beneath the Pages project URL. */
(() => {
  const files = new Map();
  function read(path) {
    if (!files.has(path)) {
      const pending = fetch(new URL(path, document.baseURI)).then(response => {
        if (!response.ok) throw new Error(`Snapshot unavailable (HTTP ${response.status}); reload to retry`);
        return response.json();
      }).catch(error => { files.delete(path); throw error; });
      files.set(path, pending);
    }
    return files.get(path);
  }
  async function manifest() {
    const result = await read('./data/manifest.json');
    if (result.schema !== 1) throw new Error('Unsupported snapshot; reload the webpage');
    return result;
  }
  const round = (value, places) => {
    // Match Python's even rounding for exact binary halfway values (e.g. 2.25).
    const scale = 10 ** places, scaled = Math.abs(value) * scale, lower = Math.floor(scaled);
    if (scaled % 1 === 0.5 && Number.isInteger(Math.abs(value) * 2 ** (places + 1))) {
      return Math.sign(value) * (lower % 2 ? lower + 1 : lower) / scale;
    }
    return Number(value.toFixed(places));
  };
  globalThis.What2CraftData = {
    async worlds() { return {data_centers: (await manifest()).data_centers}; },
    async ingredients(row) {
      const group = await read(row.detail_file);
      return group[row.recipe_index]?.[row.ingredient_quality] || [];
    },
    async query(params) {
      const meta = await manifest();
      const world = params.get('world') || 'region';
      if (!meta.scopes[world]) throw new Error('Selling world is unavailable in this snapshot');
      const [recipes, scopes] = await Promise.all([read(meta.recipes), read(meta.scopes[world])]);
      const product = params.get('product_quality') || 'auto';
      const ingredient = params.get('ingredient_quality') || 'cheapest';
      const search = (params.get('search') || '').trim().toLowerCase();
      let rows = [];
      recipes.forEach((recipe, index) => {
        const choices = scopes[index];
        if (!choices || product === 'hq' && recipe.can_be_hq === false) return;
        let selling = choices[product];
        if (typeof selling === 'string') selling = choices[selling];
        const [revenue = null, velocity = null, quality = null, productWarnings = []] = selling || [];
        const [cost = null, costWarnings = []] = recipe.costs[ingredient] || [];
        if (params.get('dataset') !== 'all' && (velocity === null || velocity < 0.5)) return;
        if (search && !recipe.item.toLowerCase().includes(search)) return;
        if (params.get('job') && recipe.crafter_job !== params.get('job')) return;
        const profit = revenue !== null && cost !== null ? revenue - cost : null;
        const values = {max_level: recipe.level, max_cost: cost, min_profit: profit === null ? null : round(profit, 1), min_demand: velocity};
        for (const [key, value] of Object.entries(values)) {
          if (params.has(key) && (value === null || (key.startsWith('min_') ? value < Number(params.get(key)) : value > Number(params.get(key))))) return;
        }
        const warnings = [...productWarnings, ...costWarnings];
        if (revenue === null || cost === null) warnings.unshift('Insufficient price data');
        rows.push({
          item: recipe.item, item_id: recipe.item_id, icon_url: recipe.icon_url,
          crafter_job: recipe.crafter_job, level: recipe.level, yield_qty: recipe.yield_qty,
          revenue, cost, product_quality: quality, sale_velocity_per_day: velocity,
          profit: profit === null ? null : round(profit, 1),
          margin_pct: profit !== null && cost ? round(profit / cost * 100, 1) : null,
          daily_profit_potential: profit !== null && velocity !== null && recipe.yield_qty > 0 ? round(profit * velocity / recipe.yield_qty, 1) : null,
          data_warnings: [...new Set(warnings)],
          market_url: `https://universalis.app/market/${recipe.item_id}`,
          recipe_index: index, ingredient_quality: ingredient,
          detail_file: `${meta.ingredients}${Math.floor(index / 100)}.json`,
        });
      });
      const key = params.get('sort_key') || 'profit';
      const direction = params.get('sort_dir') === 'asc' ? 1 : -1;
      rows.sort((a, b) => {
        const x = a[key], y = b[key];
        if (x == null || y == null) return x == null ? (y == null ? 0 : 1) : -1;
        const order = typeof x === 'number' ? x - y : String(x).toLowerCase() < String(y).toLowerCase() ? -1 : String(x).toLowerCase() > String(y).toLowerCase() ? 1 : 0;
        return order * direction;
      });
      const count = rows.length, size = 100, pages = Math.max(1, Math.ceil(count / size));
      const page = Math.max(1, Math.min(Number(params.get('page')) || 1, pages));
      rows = rows.slice((page - 1) * size, page * size);
      return {rows, count, page, total_pages: pages, updated_at: meta.updated_at,
        data_warning: meta.warning, quality_refresh_required: false};
    },
  };
})();
