const state = {
  health: null,
  products: [],
  result: null,
  visibleRows: [],
  stockoutsBySku: {},
  stockoutLabel: '',
  busy: false,
};

const byId = (id) => document.getElementById(id);
const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));
}

function fmt(value, digits = 0) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: digits }).format(Number(value));
}

function showToast(message) {
  const toast = byId('toast');
  toast.textContent = message;
  toast.classList.add('show');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove('show'), 3000);
}

function showBanner(message, kind = 'warning') {
  const banner = byId('status-banner');
  banner.textContent = message;
  banner.className = `status-banner ${kind}`;
  banner.hidden = !message;
}

async function api(path, payload) {
  const options = payload === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  };
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch { throw new Error('Сервер вернул непонятный ответ. Перезагрузите страницу.'); }
  if (!response.ok) throw new Error(data.error || 'Запрос не выполнен. Попробуйте ещё раз.');
  return data;
}

function getScenario() {
  const sku = byId('sku-input').value.trim().replace(/_+$/, '');
  return {
    supplier: byId('supplier-filter').value,
    horizon_days: Number.parseInt(byId('horizon-input').value, 10) || 30,
    demand_multiplier: 1 + (Number(byId('demand-range').value) || 0) / 100,
    selected_sku: sku,
    stock_delta: Number(byId('stock-delta').value) || 0,
    inbound_delta: Number(byId('inbound-delta').value) || 0,
    stockout_days: Number.parseInt(byId('stockout-days').value, 10) || 0,
    stockouts_by_sku: state.stockoutsBySku,
  };
}

function setBusy(busy, targetButton = null, label = '') {
  state.busy = busy;
  if (targetButton) {
    targetButton.disabled = busy;
    if (busy) {
      targetButton.dataset.previousLabel = targetButton.innerHTML;
      targetButton.innerHTML = '<span class="spinner"></span>Считаю…';
    } else if (targetButton.dataset.previousLabel) {
      targetButton.innerHTML = targetButton.dataset.previousLabel;
      delete targetButton.dataset.previousLabel;
    }
  }
  if (label) byId('updated-label').textContent = label;
}

async function calculate() {
  if (state.busy) return;
  const button = byId('scenario-run');
  setBusy(true, button, 'Расчёт обновляется');
  try {
    const result = await api('/api/recommendations', { scenario: getScenario(), stockouts_by_sku: state.stockoutsBySku });
    renderResult(result);
    renderAgentResult(null);
    showBanner(result.warnings?.join(' ') || '', result.warnings?.length ? 'warning' : 'success');
    byId('updated-label').textContent = `Пересчитано ${new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}`;
  } catch (error) {
    showBanner(error.message, 'error');
    byId('updated-label').textContent = 'Расчёт не выполнен';
  } finally {
    setBusy(false, button);
  }
}

function urgencyClass(value) {
  if (value === 'Срочно') return 'urgent';
  if (value === 'Высокий риск') return 'risk';
  return 'planned';
}

function filteredRows() {
  if (!state.result) return [];
  const supplier = byId('supplier-filter').value;
  const urgency = byId('urgency-filter').value;
  const query = byId('search-input').value.trim().toLowerCase();
  return state.result.recommendations.filter((row) => {
    if (supplier !== 'all' && row.supplier !== supplier) return false;
    if (urgency !== 'all' && row.urgency !== urgency) return false;
    if (query && !`${row.sku} ${row.supplier_sku} ${row.name}`.toLowerCase().includes(query)) return false;
    return true;
  });
}

function renderRows() {
  const rows = filteredRows();
  state.visibleRows = rows;
  const limit = 200;
  const shown = rows.slice(0, limit);
  byId('visible-count').textContent = fmt(rows.length);
  byId('table-footer-text').textContent = rows.length > limit
    ? `Показаны первые ${fmt(limit)} из ${fmt(rows.length)} позиций · отсортировано по риску дефицита`
    : 'Позиции отсортированы по риску дефицита';
  const body = byId('recommendation-rows');
  if (!shown.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty-cell">Для выбранных условий заказ не требуется. Проверьте сценарий или фильтры.</td></tr>';
    return;
  }
  body.innerHTML = shown.map((row) => `
    <tr class="data-row" data-sku="${escapeHtml(row.sku)}">
      <td class="product-cell">
        <div class="product-name" title="${escapeHtml(row.name)}">${escapeHtml(row.name || row.sku)}</div>
        <div class="product-meta"><span class="sku-code">${escapeHtml(row.sku)}</span><span class="category-dot"></span><span>${escapeHtml(row.category || 'Без категории')}</span></div>
      </td>
      <td><span class="supplier-tag">${escapeHtml(row.supplier)}</span></td>
      <td class="demand-cell">${fmt(row.forecast_units, 1)} <span>${escapeHtml(row.unit)}</span></td>
      <td class="available-cell">${fmt(row.current_stock + row.in_transit, 1)}<small>остаток + в пути</small></td>
      <td><span class="order-qty">${fmt(row.recommended_qty, 1)}</span> <span class="demand-cell">${escapeHtml(row.unit)}</span></td>
      <td><span class="urgency ${urgencyClass(row.urgency)}">${escapeHtml(row.urgency)}</span></td>
      <td><span class="row-open">›</span></td>
    </tr>`).join('');
  body.querySelectorAll('.data-row').forEach((row) => row.addEventListener('click', () => showItemDetail(row.dataset.sku)));
}

function renderResult(result) {
  state.result = result;
  const metrics = result.metrics || {};
  byId('metric-positions').textContent = fmt(metrics.recommendation_positions);
  byId('metric-units').textContent = fmt(metrics.recommended_units, 1);
  byId('metric-urgent').textContent = fmt(metrics.urgent_positions);
  byId('metric-risk').textContent = fmt(metrics.high_risk_positions);
  byId('metric-incomplete').textContent = fmt(metrics.incomplete_products);
  byId('as-of-date').textContent = new Date(`${result.as_of_date}T12:00:00`).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
  byId('snapshot-hint').textContent = `Плановый горизонт · ${fmt(result.scenario?.horizon_days || 30)} дней`;
  renderRows();
}

function showItemDetail(sku) {
  const row = state.result?.recommendations.find((item) => item.sku === sku);
  const detail = byId('item-detail');
  if (!row) return;
  detail.hidden = false;
  detail.innerHTML = `
    <h3>${escapeHtml(row.name)} · ${escapeHtml(row.sku)}</h3>
    <p>${escapeHtml(row.explanation)}</p>
    <div class="detail-facts">
      <span>Поставщик: ${escapeHtml(row.supplier)}</span>
      <span>MOQ: ${fmt(row.moq)} ${escapeHtml(row.unit)}</span>
      <span>Покрытие: ${row.cover_days === null ? '—' : `${fmt(row.cover_days, 1)} дн.`}</span>
      <span>Снимок остатка: ${escapeHtml(row.stock_snapshot || 'нет даты')}</span>
      <span>${escapeHtml(row.stock_basis || '')}</span>
    </div>`;
}

function renderAgentResult(payload) {
  const box = byId('agent-result');
  if (!payload) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const result = payload.result;
  renderResult(result);
  byId('agent-answer').textContent = payload.answer || 'Расчёт выполнен.';
  byId('agent-result-title').textContent = payload.mode === 'openai' ? 'Агент проверил расчёт' : 'Локальный расчёт проверен';
  byId('agent-trace').innerHTML = `Шаги: запрос → <span>${escapeHtml(payload.tool || 'расчётный инструмент')}</span> → проверка правил → рекомендации${payload.latency_ms ? ` · ${fmt(payload.latency_ms)} мс` : ''}`;
  if (payload.warning) showBanner(payload.warning, 'warning');
  else if (result.warnings?.length) showBanner(result.warnings.join(' '), 'warning');
  else showBanner('', 'success');
}

async function submitAgent(event) {
  event.preventDefault();
  if (state.busy) return;
  const query = byId('agent-query').value.trim();
  const button = byId('agent-submit');
  setBusy(true, button, 'Агент анализирует');
  byId('agent-result').hidden = true;
  try {
    const payload = await api('/api/agent', { query, scenario: getScenario(), stockouts_by_sku: state.stockoutsBySku });
    renderAgentResult(payload);
    const badge = byId('agent-mode-badge');
    if (payload.mode === 'openai') {
      badge.textContent = 'OPENAI';
      badge.classList.add('live');
    } else {
      badge.textContent = payload.mode === 'demo_fallback' ? 'FALLBACK' : 'DEMO';
      badge.classList.remove('live');
    }
    if (payload.mode !== 'openai' && payload.mode !== 'demo') showBanner(payload.warning || 'OpenAI недоступен. Локальный расчёт выполнен.', 'warning');
    byId('updated-label').textContent = payload.mode === 'openai' ? 'Агент завершил анализ' : 'Demo-агент завершил расчёт';
  } catch (error) {
    showBanner(error.message, 'error');
    byId('updated-label').textContent = 'Агент не завершил запрос';
  } finally {
    setBusy(false, button);
  }
}

function renderSources() {
  const sources = state.health?.sources || [];
  byId('catalog-count').textContent = fmt(state.health?.products || 0);
  byId('source-cards').innerHTML = sources.map((source) => `
    <div class="source-card">
      <div class="source-info"><strong>${escapeHtml(source.supplier)}</strong><small>${fmt(source.with_sales_history)} с историей · ${fmt(source.with_stock)} с остатком</small></div>
      <span class="source-count">${fmt(source.products)} SKU</span>
    </div>`).join('');
  const limits = state.health?.data_limits || [];
  byId('data-quality').innerHTML = `
    <div class="quality-title"><span class="quality-icon">◇</span>Ограничения входных данных</div>
    ${limits.map((limit) => `<p class="quality-copy">${escapeHtml(limit)}</p>`).join('')}`;
  if (state.health?.openai_enabled) {
    byId('agent-mode-badge').textContent = 'OPENAI';
    byId('agent-mode-badge').classList.add('live');
    byId('agent-status-label').textContent = 'OpenAI подключён';
  } else {
    byId('agent-mode-badge').textContent = 'DEMO';
    byId('agent-mode-badge').classList.remove('live');
    byId('agent-status-label').textContent = 'Локальный режим';
  }
}

async function loadProducts() {
  const data = await api('/api/products');
  state.products = data.products || [];
  byId('sku-options').innerHTML = state.products.map((item) => `<option value="${escapeHtml(item.sku)}">${escapeHtml(`${item.supplier} · ${item.name}`)}</option>`).join('');
}

function parseStockoutCsv(text) {
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/).filter((line) => line.trim());
  if (!lines.length) throw new Error('CSV пустой.');
  const separator = lines[0].includes(';') ? ';' : ',';
  const header = lines[0].split(separator).map((part) => part.trim().toLowerCase());
  const skuCol = header.indexOf('sku');
  const monthCol = header.indexOf('month');
  const daysCol = header.indexOf('days_out');
  if (skuCol < 0 || monthCol < 0 || daysCol < 0) throw new Error('Ожидаются заголовки: sku,month,days_out.');
  const result = {};
  let validRows = 0;
  for (const line of lines.slice(1)) {
    const cells = line.split(separator).map((part) => part.trim().replace(/^"|"$/g, ''));
    const sku = (cells[skuCol] || '').replace(/_+$/, '');
    const month = cells[monthCol];
    const days = Number.parseInt(cells[daysCol], 10);
    if (!sku || !/^20\d{2}-(0[1-9]|1[0-2])$/.test(month) || !Number.isInteger(days)) continue;
    result[sku] ||= {};
    result[sku][month] = days;
    validRows += 1;
  }
  if (!validRows) throw new Error('В CSV не найдено ни одной подходящей строки.');
  return { result, validRows };
}

function exportCsv() {
  const rows = state.visibleRows;
  if (!rows.length) { showToast('Нет строк для экспорта.'); return; }
  const headers = ['Артикул 1С', 'Артикул поставщика', 'Наименование', 'Поставщик', 'Категория', 'Прогноз спроса', 'Остаток', 'В пути', 'MOQ', 'Рекомендуемое количество', 'Срочность', 'Обоснование'];
  const fields = ['sku', 'supplier_sku', 'name', 'supplier', 'category', 'forecast_units', 'current_stock', 'in_transit', 'moq', 'recommended_qty', 'urgency', 'explanation'];
  const quote = (value) => `"${String(value ?? '').replace(/"/g, '""')}"`;
  const csv = '\uFEFF' + [headers, ...rows.map((row) => fields.map((field) => row[field]))].map((line) => line.map(quote).join(';')).join('\r\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `plan-popolneniya-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(link.href);
  showToast(`Экспортировано строк: ${fmt(rows.length)}`);
}

function initEvents() {
  byId('supplier-filter').addEventListener('change', () => {
    renderRows();
    calculate();
  });
  byId('urgency-filter').addEventListener('change', renderRows);
  byId('search-input').addEventListener('input', renderRows);
  byId('refresh-button').addEventListener('click', calculate);
  byId('scenario-run').addEventListener('click', calculate);
  byId('export-button').addEventListener('click', exportCsv);
  byId('agent-form').addEventListener('submit', submitAgent);
  document.querySelectorAll('.suggestion').forEach((button) => button.addEventListener('click', () => {
    byId('agent-query').value = button.dataset.prompt || '';
    byId('agent-query').focus();
  }));
  byId('demand-range').addEventListener('input', (event) => { byId('demand-value').value = `${event.target.value}%`; });
  byId('scenario-toggle').addEventListener('click', () => {
    const button = byId('scenario-toggle');
    const open = button.getAttribute('aria-expanded') !== 'true';
    button.setAttribute('aria-expanded', String(open));
    byId('scenario-body').hidden = !open;
  });
  byId('show-incomplete').addEventListener('click', () => {
    const items = state.result?.incomplete || [];
    const shown = items.slice(0, 10).map((item) => `${item.supplier} · ${item.sku} — нет поля: ${item.missing}`).join('\n');
    const detail = byId('item-detail');
    detail.hidden = false;
    detail.innerHTML = `<h3>Позиции, которые нельзя посчитать без уточнения источника</h3><p>${escapeHtml(shown || 'Все рассчитанные позиции содержат обязательные поля.')}${items.length > 10 ? `\n… и ещё ${fmt(items.length - 10)} SKU` : ''}</p>`;
    detail.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  });
  byId('stockout-file').addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const parsed = parseStockoutCsv(await file.text());
      state.stockoutsBySku = parsed.result;
      state.stockoutLabel = file.name;
      byId('stockout-file-note').textContent = `${file.name} · строк: ${fmt(parsed.validRows)}`;
      showToast('Stockout-периоды загружены. Пересчитайте сценарий.');
    } catch (error) {
      byId('stockout-file-note').textContent = error.message;
      showBanner(error.message, 'error');
    }
  });
}

async function start() {
  initEvents();
  try {
    state.health = await api('/api/health');
    renderSources();
    await loadProducts();
    await calculate();
  } catch (error) {
    showBanner(error.message, 'error');
    byId('recommendation-rows').innerHTML = `<tr><td colspan="7" class="empty-cell">${escapeHtml(error.message)}</td></tr>`;
    byId('updated-label').textContent = 'Не удалось загрузить каталог';
  }
}

start();
