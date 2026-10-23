const state = {
  dataset: "liquid",
  world: "region",
  productQuality: "auto",
  ingredientQuality: "cheapest",
  qualityRefreshRequired: false,
  dataWarning: null,
  loadedQuery: null,
  job: "",
  minProfit: null,
  minDemand: null,
  rows: [],
  search: "",
  maxLevel: null,
  maxCost: null,
  sortKey: "profit",
  sortDir: "desc",
  updatedAt: null,
  page: 1,
  pageSize: 100,
  pageCount: 1,
  totalCount: 0,
  loading: false,
};

const preferenceKeys = ["dataset", "world", "productQuality", "ingredientQuality", "search", "maxLevel", "maxCost", "sortKey", "sortDir", "job", "minProfit", "minDemand"];
const defaultPreferences = Object.fromEntries(preferenceKeys.map(key => [key, state[key]]));
const preferenceStorageKey = "ffxiv-market-preferences-v1";
function savePreferences() {
  try { localStorage.setItem(preferenceStorageKey, JSON.stringify(Object.fromEntries(preferenceKeys.map(key => [key, state[key]])))); } catch {}
}
function restorePreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(preferenceStorageKey));
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return;
    const choices = {dataset: ["liquid", "all"], productQuality: ["auto", "nq", "hq"], ingredientQuality: ["cheapest", "nq", "hq"], sortDir: ["asc", "desc"], job: ["", "CRP", "BSM", "ARM", "GSM", "LTW", "WVR", "ALC", "CUL"], sortKey: ["item", "profit", "cost", "margin_pct", "sale_velocity_per_day", "daily_profit_potential", "crafter_job", "level", "yield_qty"]};
    for (const key of preferenceKeys) {
      const value = saved[key];
      if (choices[key]) { if (choices[key].includes(value)) state[key] = value; }
      else if (["maxLevel", "maxCost", "minProfit", "minDemand"].includes(key)) {
        if (value === null || typeof value === "number" && Number.isFinite(value) && value >= 0) state[key] = value;
      } else if (typeof value === "string") state[key] = value;
    }
  } catch {}
}
restorePreferences();
function syncControls() {
  const controls = {"search-box": "search", "max-level-input": "maxLevel", "max-cost-input": "maxCost", "world-select": "world", "product-quality": "productQuality", "ingredient-quality": "ingredientQuality", "job-filter": "job", "min-profit": "minProfit", "min-demand": "minDemand"};
  for (const [id, key] of Object.entries(controls)) document.getElementById(id).value = state[key] ?? "";
  document.querySelectorAll(".toggle-btn").forEach(btn => btn.classList[btn.dataset.dataset === state.dataset ? "add" : "remove"]("active"));
}

const numberFmt = new Intl.NumberFormat("en-US", {maximumFractionDigits: 0});

function fmtCell(key, value) {
  if (value === "" || value === null || value === undefined) return "—";
  if (key === "margin_pct") return `${Number(value).toFixed(1)}%`;
  if (key === "sale_velocity_per_day") return Number(value).toFixed(1);
  if (typeof value === "number") return numberFmt.format(value);
  return value;
}

function render() {
  savePreferences();
  const tbody = document.getElementById("data-body");
  tbody.innerHTML = "";
  const frag = document.createDocumentFragment();
  const rows = state.rows;

  for (const row of rows) {
    const tr = document.createElement("tr");
    const cols = ["item", "profit", "cost", "margin_pct", "sale_velocity_per_day", "daily_profit_potential", "crafter_job", "level", "yield_qty"];
    for (const key of cols) {
      const td = document.createElement("td");
      if (["level", "yield_qty", "cost", "revenue", "profit", "margin_pct", "sale_velocity_per_day", "daily_profit_potential"].includes(key)) {
        td.classList.add("num");
      }
      if (key === "profit" && row[key] > 0) td.classList.add("profit-positive");
      if (key === "cost") {
        td.classList.add("cost-cell");
        td._ingredients = row.ingredients;
        td._row = row;
      }
      if (key === "item") {
        td.classList.add("item-cell");
        if (row.icon_url) {
          const img = document.createElement("img");
          img.src = row.icon_url;
          img.alt = "";
          img.loading = "lazy";
          img.className = "item-icon";
          td.appendChild(img);
        }
        const link = document.createElement("a");
        link.href = row.market_url || `https://universalis.app/market/${row.item_id}`;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.title = "Verify prices on Universalis";
        link.textContent = row.item;
        td.appendChild(link);
        if (row.product_quality === "hq" || row.product_quality === "combined") {
          const badge = document.createElement("span");
          badge.className = "quality-badge";
          badge.textContent = row.product_quality === "hq" ? "\u2726 HQ" : "NQ/HQ combined";
          badge.title = row.product_quality === "hq" ? "High quality product pricing and demand" : "Combined NQ and HQ estimate";
          td.appendChild(badge);
        }
        if (row.data_warnings?.length) {
          const warning = document.createElement("span");
          warning.className = "data-warning";
          warning.textContent = "\u26a0\ufe0e";
          warning.title = row.data_warnings.join("\n");
          warning.tabIndex = 0;
          warning.setAttribute("role", "img");
          warning.setAttribute("aria-label", `Warning: ${row.data_warnings.join("; ")}`);
          td.appendChild(warning);
        }
      } else {
        td.textContent = fmtCell(key, row[key]);
      }
      if (key === "daily_profit_potential") td.title = "Profit per craft × units/day ÷ yield. Assumes capturing all observed sales; not expected earnings.";
      tr.appendChild(td);
    }
    frag.appendChild(tr);
  }
  tbody.appendChild(frag);
  updateMetaLine(rows.length);
  for (const suffix of ["", "-bottom"]) {
    document.getElementById(`page-status${suffix}`).textContent = `Page ${state.page} of ${state.pageCount}`;
    document.getElementById(`page-prev${suffix}`).disabled = state.loading || !state.loadedQuery || state.page <= 1;
    document.getElementById(`page-next${suffix}`).disabled = state.loading || !state.loadedQuery || state.page >= state.pageCount;
  }
  document.getElementById("data-table").setAttribute("aria-busy", String(state.loading));

  document.querySelectorAll("thead th").forEach((th) => {
    th.querySelector(".arrow")?.remove();
    if (th.dataset.key === state.sortKey) {
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = state.sortDir === "asc" ? "▲" : "▼";
      th.appendChild(arrow);
    }
  });
}

let dataRequestId = 0;
let filterTimer = null;
function scheduleLoad() {
  clearTimeout(filterTimer);
  ++dataRequestId; // A response for the previous input must not replace this query.
  state.rows = [];
  state.loadedQuery = null;
  state.loading = true;
  state.dataWarning = "Loading results...";
  render();
  filterTimer = setTimeout(() => loadData(), 250);
}
async function loadData({page = 1} = {}) {
  clearTimeout(filterTimer);
  const requestId = ++dataRequestId;
  const params = new URLSearchParams({dataset: state.dataset, world: state.world,
    product_quality: state.productQuality, ingredient_quality: state.ingredientQuality,
    page, page_size: state.pageSize, sort_key: state.sortKey, sort_dir: state.sortDir,
    search: state.search, job: state.job});
  for (const [key, value] of [["max_level", state.maxLevel], ["max_cost", state.maxCost],
    ["min_profit", state.minProfit], ["min_demand", state.minDemand]]) {
    if (value !== null) params.set(key, value);
  }
  const query = params.toString();
  if (state.loadedQuery !== query) {
    state.rows = [];
    state.totalCount = 0;
    state.updatedAt = null;
    state.qualityRefreshRequired = false;
    state.loadedQuery = null;
  }
  state.dataWarning = "Loading results...";
  state.loading = true;
  render();
  try {
    const resp = globalThis.What2CraftData
      ? {ok: true, json: () => globalThis.What2CraftData.query(params)}
      : await fetch(`/api/data?${params}`);
    if (!resp.ok) {
      let message = `Data request failed (HTTP ${resp.status})`;
      try { message = (await resp.json()).error || message; } catch {}
      throw new Error(message);
    }
    const payload = await resp.json();
    if (requestId !== dataRequestId) return;
    if (!Array.isArray(payload.rows)) throw new Error("Invalid results response");
    state.rows = payload.rows;
    state.loadedQuery = query;
    state.dataWarning = payload.data_warning || null;
    state.totalCount = payload.count;
    state.page = payload.page ?? page;
    state.pageCount = payload.total_pages ?? 1;
    state.updatedAt = payload.updated_at;
    state.qualityRefreshRequired = payload.quality_refresh_required;
    state.loading = false;
    render();
  } catch (error) {
    if (requestId !== dataRequestId) return;
    state.loading = false;
    state.dataWarning = `${state.loadedQuery === query ? "Showing previous results for these filters. " : ""}Unable to load results: ${error.message}`;
    render();
  }
}
for (const suffix of ["", "-bottom"]) {
  document.getElementById(`page-prev${suffix}`).addEventListener("click", () => {
    if (!state.loading && state.loadedQuery && state.page > 1) loadData({page: state.page - 1});
  });
  document.getElementById(`page-next${suffix}`).addEventListener("click", () => {
    if (!state.loading && state.loadedQuery && state.page < state.pageCount) loadData({page: state.page + 1});
  });
}
for (const [id, key] of [["product-quality", "productQuality"], ["ingredient-quality", "ingredientQuality"]]) {
  document.getElementById(id).addEventListener("change", (event) => {
    state[key] = event.target.value;
    loadData();
  });
}

async function loadWorlds() {
  const resp = globalThis.What2CraftData
    ? {ok: true, json: () => globalThis.What2CraftData.worlds()}
    : await fetch("/api/worlds");
  if (!resp.ok) throw new Error(`World list request failed (HTTP ${resp.status})`);
  const payload = await resp.json();
  const select = document.getElementById("world-select");
  const validWorlds = Object.values(payload.data_centers).flat();
  if (state.world !== "region" && !validWorlds.includes(state.world)) state.world = "region";
  for (const [dc, worlds] of Object.entries(payload.data_centers)) {
    const group = document.createElement("optgroup");
    group.label = dc;
    for (const world of worlds) {
      const opt = document.createElement("option");
      opt.value = world;
      opt.textContent = world;
      group.appendChild(opt);
    }
    select.appendChild(group);
  }
  syncControls();
}

function updateMetaLine(visibleCount) {
  const meta = document.getElementById("meta-line");
  const when = state.updatedAt ? new Date(state.updatedAt * 1000).toLocaleString() : "unknown";
  const start = visibleCount ? (state.page - 1) * state.pageSize + 1 : 0;
  const countText = `${start}–${visibleCount ? start + visibleCount - 1 : 0} of ${state.totalCount} matching items`;
  meta.textContent = state.dataWarning ? state.dataWarning : state.qualityRefreshRequired
    ? "Saved estimates need rebuilding with current pricing rules. Refresh prices to update them."
    : `${countText} · report generated ${when} · All craftable items includes unavailable estimates.`;
}

document.querySelectorAll("thead th").forEach((th) => {
  if (!th.dataset.key) return;
  th.addEventListener("click", () => {
    const key = th.dataset.key;
    if (state.sortKey === key) {
      state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    } else {
      state.sortKey = key;
      state.sortDir = "desc";
    }
    loadData();
  });
});

document.querySelectorAll(".toggle-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".toggle-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.dataset = btn.dataset.dataset;
    loadData();
  });
});

const searchBox = document.getElementById("search-box");
searchBox.addEventListener("input", () => {
  state.search = searchBox.value.trim();
  scheduleLoad();
});

const maxLevelInput = document.getElementById("max-level-input");
maxLevelInput.addEventListener("input", () => {
  state.maxLevel = maxLevelInput.value === "" ? null : Number(maxLevelInput.value);
  scheduleLoad();
});

const maxCostInput = document.getElementById("max-cost-input");
maxCostInput.addEventListener("input", () => {
  state.maxCost = maxCostInput.value === "" ? null : Number(maxCostInput.value);
  scheduleLoad();
});

const worldSelect = document.getElementById("world-select");
worldSelect.addEventListener("change", () => {
  state.world = worldSelect.value;
  loadData();
});

for (const [id, key] of [["job-filter", "job"]]) {
  document.getElementById(id).addEventListener("change", event => {
    state[key] = event.target.value;
    loadData();
  });
}
for (const [id, key] of [["min-profit", "minProfit"], ["min-demand", "minDemand"]]) {
  document.getElementById(id).addEventListener("input", event => {
    const value = Number(event.target.value);
    state[key] = event.target.value === "" || !Number.isFinite(value) || value < 0 ? null : value;
    scheduleLoad();
  });
}
document.getElementById("reset-filters").addEventListener("click", () => {
  Object.assign(state, defaultPreferences);
  syncControls();
  loadData();
});

const refreshBtn = document.getElementById("refresh-btn");
const refreshStatus = document.getElementById("refresh-status");
const progressWrap = document.getElementById("refresh-progress-wrap");
const progressBar = document.getElementById("refresh-progress-bar");
const progressText = document.getElementById("refresh-progress-text");
let pollTimer = null;
let tickTimer = null;
let refreshStartedAt = null;
let lastProgress = null;
let statusPolling = false;
let statusGeneration = 0;
let refreshStarting = false;

function formatElapsed(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function updateProgressDisplay(progress) {
  const elapsedSec = refreshStartedAt ? (Date.now() / 1000 - refreshStartedAt) : 0;
  if (progress && progress.total) {
    const pct = Math.min(100, (progress.done / progress.total) * 100);
    progressBar.style.width = `${pct}%`;
    progressText.textContent = `${progress.done} / ${progress.total} batches${progress.failed ? ` · ${progress.failed} failed` : ""} · ${formatElapsed(elapsedSec)} elapsed`;
  } else {
    progressBar.style.width = "0%";
    progressText.textContent = `${formatElapsed(elapsedSec)} elapsed`;
  }
}

function stopProgress() {
  clearInterval(tickTimer);
  tickTimer = null;
  lastProgress = null;
  progressWrap.hidden = true;
  progressBar.style.width = "0%";
}

function startPolling() {
  refreshBtn.disabled = true;
  progressWrap.hidden = false;
  if (!tickTimer) tickTimer = setInterval(() => updateProgressDisplay(lastProgress), 1000);
  if (!pollTimer) pollTimer = setInterval(pollRefreshStatus, 2000);
}
async function pollRefreshStatus() {
  if (statusPolling || refreshStarting) return;
  statusPolling = true;
  const generation = statusGeneration;
  try {
    const resp = await fetch("/api/refresh/status");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const s = await resp.json();
    if (generation !== statusGeneration) return;
    if (!["idle", "running", "done", "error"].includes(s.status)) throw new Error("Invalid refresh status");
    if (s.status === "running") {
      startPolling();
      refreshStatus.textContent = `Refreshing (${s.step})...`;
      refreshStatus.className = "";
      refreshStartedAt = s.started_at;
      lastProgress = s.progress;
      updateProgressDisplay(lastProgress);
    } else {
      clearInterval(pollTimer);
      pollTimer = null;
      stopProgress();
      refreshBtn.disabled = false;
      if (s.status === "idle") {
        refreshStatus.textContent = "";
        refreshStatus.className = "";
      }
      if (s.status === "done") {
        refreshStatus.textContent = s.warning ? `Prices updated. ${s.warning}` : "Prices updated.";
        refreshStatus.className = "done";
        await loadData();
      } else if (s.status === "error") {
        refreshStatus.textContent = `Refresh failed: ${s.error}`;
        refreshStatus.className = "error";
      }
    }
  } catch (error) {
    if (generation !== statusGeneration) return;
    refreshStatus.textContent = `Status unavailable; retrying: ${error.message}`;
    startPolling();
  } finally {
    if (generation === statusGeneration) statusPolling = false;
  }
}
refreshBtn?.addEventListener("click", async () => {
  if (refreshStarting) return;
  refreshStarting = true;
  statusGeneration++;
  statusPolling = false;
  refreshBtn.disabled = true;
  refreshStatus.textContent = "Starting refresh...";
  refreshStatus.className = "";
  try {
    const resp = await fetch("/api/refresh", {method: "POST"});
    if (!resp.ok && resp.status !== 409) throw new Error(`HTTP ${resp.status}`);
  } catch (error) {
    refreshStatus.textContent = `Refresh request failed: ${error.message}. Checking status...`;
  } finally {
    refreshStarting = false;
    startPolling();
    await pollRefreshStatus();
  }
});

const popover = document.createElement("div");
popover.id = "ingredient-popover";
popover.hidden = true;
document.body.appendChild(popover);

function renderPopover(ingredients) {
  popover.innerHTML = "";
  const list = document.createElement("ul");
  for (const ing of ingredients) {
    const li = document.createElement("li");
    if (ing.icon_url) {
      const img = document.createElement("img");
      img.src = ing.icon_url;
      img.alt = "";
      li.appendChild(img);
    }
    const name = document.createElement("span");
    name.className = "ing-name";
    const quality = ing.quality === "hq" ? " HQ" : ing.quality === "mixed" ? " NQ/HQ" : "";
    name.textContent = `${ing.name}${quality} ×${ing.quantity}`;
    const price = document.createElement("span");
    price.className = "ing-price";
    price.textContent = `${numberFmt.format(ing.subtotal)}`;
    li.appendChild(name);
    li.appendChild(price);
    list.appendChild(li);
  }
  popover.appendChild(list);
}

let popoverTarget = null;
async function showPopover(td) {
  if (popoverTarget === td && !popover.hidden) return;
  popoverTarget = td;
  popover.hidden = true;
  let ingredients = td._ingredients;
  if (!ingredients && globalThis.What2CraftData) {
    try {
      ingredients = await globalThis.What2CraftData.ingredients(td._row);
      td._ingredients = ingredients;
    } catch {
      if (popoverTarget === td) popover.hidden = true;
      return;
    }
    if (popoverTarget !== td || td.isConnected === false) return;
  }
  if (!ingredients || !ingredients.length) return;
  renderPopover(ingredients);
  popover.hidden = false;

  const rect = td.getBoundingClientRect();
  const popRect = popover.getBoundingClientRect();
  let left = rect.right + 8;
  let top = rect.top;
  if (left + popRect.width > window.innerWidth) left = rect.left - popRect.width - 8;
  if (top + popRect.height > window.innerHeight) top = window.innerHeight - popRect.height - 8;
  popover.style.left = `${Math.max(8, left)}px`;
  popover.style.top = `${Math.max(8, top)}px`;
}

function hidePopover() {
  popoverTarget = null;
  popover.hidden = true;
}

const dataBody = document.getElementById("data-body");
dataBody.addEventListener("mouseover", (e) => {
  const td = e.target.closest("td.cost-cell");
  if (td) showPopover(td);
});
dataBody.addEventListener("mouseout", (e) => {
  const td = e.target.closest("td.cost-cell");
  if (td && !td.contains(e.relatedTarget)) hidePopover();
});

syncControls();
loadWorlds().then(loadData).catch(error => { state.dataWarning = `World list unavailable: ${error.message}`; loadData(); });

if (refreshBtn) pollRefreshStatus();
