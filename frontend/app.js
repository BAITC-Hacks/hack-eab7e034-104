const state = {
  health: null,
  products: [],
  result: null,
  previousScenario: null,
  visibleRows: [],
  stockoutsBySku: {},
  stockoutLabel: '',
  busy: false,
  approved: false,
  approvedAt: null,
  edits: {},
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
  const sku = byId('sku-input').value.trim();
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
    state.approved = false;
    state.approvedAt = null;
    updateApprovalUi();
    if (state.result && JSON.stringify(state.result.scenario) !== JSON.stringify(result.scenario)) {
      state.previousScenario = state.result.scenario;
    }
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
  const category = byId('category-filter')?.value || 'all';
  const query = byId('search-input').value.trim().toLowerCase();
  return state.result.recommendations.filter((row) => {
    if (supplier !== 'all' && row.supplier !== supplier) return false;
    if (urgency !== 'all' && row.urgency !== urgency) return false;
    if (category !== 'all' && String(row.category || 'Без категории') !== category) return false;
    if (query && !`${row.sku} ${row.supplier_sku} ${row.name}`.toLowerCase().includes(query)) return false;
    return true;
  });
}

function renderRows() {
  const rows = filteredRows();
  state.visibleRows = rows;
  byId('visible-count').textContent = fmt(rows.length);
  byId('table-footer-text').textContent = `Показаны все ${fmt(rows.length)} позиций · группировка по поставщику · внутри группы сначала позиции с высоким риском`;
  const body = byId('recommendation-rows');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty-cell">Для выбранных условий заказ не требуется. Проверьте сценарий или фильтры.</td></tr>';
    return;
  }
  let previousSupplier = '';
  body.innerHTML = rows.map((row) => {
    const supplierHeader = row.supplier !== previousSupplier
      ? `<tr class="supplier-divider"><td colspan="7">${escapeHtml(row.supplier)}</td></tr>`
      : '';
    previousSupplier = row.supplier;
    return `${supplierHeader}
    <tr class="data-row" data-sku="${escapeHtml(row.sku)}">
      <td class="product-cell">
        <div class="product-name" title="${escapeHtml(row.name)}">${escapeHtml(row.name || row.sku)}</div>
        <div class="product-meta"><span class="sku-code">${escapeHtml(row.sku)}</span><span class="category-dot"></span><span>${escapeHtml(row.category || 'Без категории')}</span></div>
      </td>
      <td><span class="supplier-tag">${escapeHtml(row.supplier)}</span></td>
      <td class="demand-cell">${fmt(row.forecast_units, 1)} <span>${escapeHtml(row.unit)}</span></td>
      <td class="available-cell">${fmt(row.current_stock + row.in_transit, 1)}<small>остаток + в пути</small></td>
      <td>
        <input class="order-edit" data-sku="${escapeHtml(row.sku)}" type="number" min="0" step="1"
          value="${escapeHtml(state.edits[row.sku] ?? row.recommended_qty)}" aria-label="Количество к заказу ${escapeHtml(row.sku)}" />
        <span class="demand-cell">${escapeHtml(row.unit)}</span>
      </td>
      <td><span class="urgency ${urgencyClass(row.urgency)}">${escapeHtml(row.urgency)}</span></td>
      <td><button class="row-open" type="button" tabindex="-1" aria-label="Открыть разбор рекомендации">›</button></td>
    </tr>`;
  }).join('');
  body.querySelectorAll('.data-row').forEach((row) => row.addEventListener('click', () => showItemDetail(row.dataset.sku)));
  body.querySelectorAll('.order-edit').forEach((input) => {
    input.addEventListener('click', (event) => event.stopPropagation());
    input.addEventListener('input', (event) => {
      event.stopPropagation();
      const value = Math.max(0, Number(event.target.value) || 0);
      state.edits[event.target.dataset.sku] = value;
      state.approved = false;
      state.approvedAt = null;
      updateApprovalUi();
    });
  });
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

function applyScenarioToControls(scenario) {
  if (!scenario) return;
  byId('supplier-filter').value = scenario.supplier || 'all';
  byId('horizon-input').value = scenario.horizon_days || 30;
  const demandPercent = Math.round(((scenario.demand_multiplier || 1) - 1) * 100);
  byId('demand-range').value = String(demandPercent);
  byId('demand-value').value = `${demandPercent}%`;
  byId('sku-input').value = scenario.selected_sku || '';
  byId('stock-delta').value = scenario.stock_delta || 0;
  byId('inbound-delta').value = scenario.inbound_delta || 0;
  byId('stockout-days').value = scenario.stockout_days || 0;
}

function closeItemModal() {
  const modal = byId('item-modal');
  if (!modal) return;
  modal.hidden = true;
  document.body.classList.remove('modal-open');
}

function showItemDetail(sku) {
  const row = state.result?.recommendations.find((item) => item.sku === sku);
  const modal = byId('item-modal');
  const detail = byId('item-modal-content');
  if (!row || !modal || !detail) return;

  const managerQty = Number(state.edits[row.sku] ?? row.recommended_qty);
  detail.innerHTML = `
    <div class="item-modal-head">
      <div>
        <div class="section-overline">РАЗБОР РЕКОМЕНДАЦИИ</div>
        <h2>${escapeHtml(row.name || row.sku)}</h2>
        <div class="product-meta"><span class="sku-code">${escapeHtml(row.sku)}</span><span class="category-dot"></span><span>${escapeHtml(row.category || 'Без категории')}</span></div>
      </div>
      <button class="modal-close" type="button" aria-label="Закрыть">×</button>
    </div>

    <div class="item-modal-grid">
      <div><small>Поставщик</small><strong>${escapeHtml(row.supplier)}</strong></div>
      <div><small>Прогноз</small><strong>${fmt(row.forecast_units, 1)} ${escapeHtml(row.unit)}</strong></div>
      <div><small>Остаток</small><strong>${fmt(row.current_stock, 1)} ${escapeHtml(row.unit)}</strong></div>
      <div><small>В пути</small><strong>${fmt(row.in_transit, 1)} ${escapeHtml(row.unit)}</strong></div>
      <div><small>Рекомендация</small><strong>${fmt(row.recommended_qty, 1)} ${escapeHtml(row.unit)}</strong></div>
      <div><small>Количество менеджера</small><strong>${fmt(managerQty, 1)} ${escapeHtml(row.unit)}</strong></div>
      <div><small>MOQ</small><strong>${fmt(row.moq, 1)} ${escapeHtml(row.unit)}</strong></div>
      <div><small>Приоритет</small><strong>${escapeHtml(row.urgency)}</strong></div>
      <div><small>Покрытие</small><strong>${row.cover_days === null ? '—' : `${fmt(row.cover_days, 1)} дн.`}</strong></div>
      <div><small>Сезонность</small><strong>${fmt(row.seasonality_factor, 2)}×</strong></div>
      <div><small>Тренд</small><strong>${fmt(row.growth_factor, 2)}×</strong></div>
      <div><small>Stockout-поправка</small><strong>+${fmt(row.stockout_compensation_units, 1)}</strong></div>
    </div>

    <div class="item-explanation">
      <strong>Почему система предлагает это количество</strong>
      <p>${escapeHtml(row.explanation)}</p>
    </div>

    <div class="item-source-note">
      <span>Снимок остатка: ${escapeHtml(row.stock_snapshot || 'дата не указана')}</span>
      <span>${escapeHtml(row.stock_basis || '')}</span>
    </div>
  `;

  detail.querySelector('.modal-close').addEventListener('click', closeItemModal);
  modal.hidden = false;
  document.body.classList.add('modal-open');
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
  if (payload.intent === 'compare_replenishment_scenarios') {
    state.previousScenario = payload.details?.baseline?.scenario || state.previousScenario;
    applyScenarioToControls(result.scenario);
  }
  byId('agent-answer').textContent = payload.answer || 'Расчёт выполнен.';
  const facts = Array.isArray(payload.facts) ? payload.facts : [];
  const factList = byId('agent-facts');
  factList.innerHTML = facts.map((fact) => `<li>${escapeHtml(fact)}</li>`).join('');
  factList.hidden = facts.length === 0;
  byId('agent-result-title').textContent = payload.mode === 'openai' ? 'Агент проверил расчёт' : 'Локальный расчёт проверен';
  byId('agent-trace').innerHTML = `Шаги: запрос → <span>${escapeHtml(payload.tool || 'расчётный инструмент')}</span> → проверка правил → результат для менеджера${payload.latency_ms ? ` · ${fmt(payload.latency_ms)} мс` : ''}`;
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
    const payload = await api('/api/agent', {
      query,
      scenario: getScenario(),
      baseline_scenario: state.previousScenario || state.result?.scenario || getScenario(),
      stockouts_by_sku: state.stockoutsBySku,
    });
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
  const categories = [...new Set(state.products.map((item) => String(item.category || 'Без категории')))].sort((a,b) => a.localeCompare(b, 'ru'));
  byId('category-filter').innerHTML = '<option value="all">Все категории</option>' + categories.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join('');
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

function updateApprovalUi() {
  const status = byId('approval-status');
  const button = byId('approve-button');
  if (!status || !button) return;
  if (state.approved) {
    const time = state.approvedAt ? new Date(state.approvedAt).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'}) : '';
    status.textContent = `Утверждено менеджером${time ? ' · ' + time : ''}`;
    status.classList.add('approved');
    button.textContent = '✓ Утверждено';
  } else {
    status.textContent = 'Черновик · требует подтверждения';
    status.classList.remove('approved');
    button.textContent = 'Утвердить план';
  }
}

function approvePlan() {
  const rows = state.visibleRows;
  if (!rows.length) { showToast('Нет строк для утверждения.'); return; }
  state.approved = true;
  state.approvedAt = new Date().toISOString();
  const snapshot = {
    approved_at: state.approvedAt,
    scenario: state.result?.scenario || getScenario(),
    items: rows.map((row) => ({
      sku: row.sku,
      supplier: row.supplier,
      recommended_qty: row.recommended_qty,
      approved_qty: Number(state.edits[row.sku] ?? row.recommended_qty),
    })),
  };
  try { localStorage.setItem('stockpilot_last_approval', JSON.stringify(snapshot)); } catch {}
  updateApprovalUi();
  showToast(`План утверждён менеджером · ${rows.length} позиций`);
}

function exportCsv() {
  const rows = state.visibleRows;
  if (!rows.length) { showToast('Нет строк для экспорта.'); return; }
  const headers = ['Артикул 1С', 'Артикул поставщика', 'Наименование', 'Поставщик', 'Категория', 'Прогноз спроса', 'Остаток', 'В пути', 'MOQ', 'Рекомендуемое количество', 'Срочность', 'Обоснование', 'Статус согласования'];
  const fields = ['sku', 'supplier_sku', 'name', 'supplier', 'category', 'forecast_units', 'current_stock', 'in_transit', 'moq'];
  const quote = (value) => `"${String(value ?? '').replace(/"/g, '""')}"`;
  const status = state.approved ? 'Утверждено менеджером' : 'Требует ручного согласования';
  const csv = '\uFEFF' + [headers, ...rows.map((row) => [
    ...fields.map((field) => row[field]),
    Number(state.edits[row.sku] ?? row.recommended_qty),
    row.urgency,
    row.explanation,
    status
  ])].map((line) => line.map(quote).join(';')).join('\r\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `${state.approved ? 'approved-order' : 'draft-order'}-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(link.href);
  showToast(state.approved
    ? `Утверждённый CSV готов · ${fmt(rows.length)} строк`
    : `Черновик CSV готов · ${fmt(rows.length)} строк требуют проверки`);
}

function initEvents() {
  byId('item-modal').addEventListener('click', (event) => {
    if (event.target === byId('item-modal')) closeItemModal();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeItemModal();
  });
  byId('supplier-filter').addEventListener('change', () => {
    renderRows();
    calculate();
  });
  byId('urgency-filter').addEventListener('change', renderRows);
  byId('category-filter').addEventListener('change', renderRows);
  byId('search-input').addEventListener('input', renderRows);
  byId('refresh-button').addEventListener('click', calculate);
  byId('scenario-run').addEventListener('click', calculate);
  byId('export-button').addEventListener('click', exportCsv);
  byId('approve-button').addEventListener('click', approvePlan);
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
  updateApprovalUi();
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
