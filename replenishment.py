"""Auditable demand forecasting; proposals are not supplier orders.

Units are stock units internally. Unknown inputs never become verified zeroes.
Planning policy is explicit, not claimed to be the partner's official policy.
"""
from __future__ import annotations
import calendar
import math
import statistics as st
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
from typing import Any

VERSION = '2.0-audit'
URGENCY_ORDER = {'Срочно': 0, 'Высокий риск': 1, 'Планово': 2}


def _finite_number(value: Any, default=None):
    if isinstance(value, bool) or value is None:
        return default
    try:
        x = float(str(value).replace('\u00a0', '').replace(' ', '').replace(',', '.'))
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _month(value):
    try:
        y, m = map(int, str(value).split('-'))
        return (y, m) if 1900 <= y <= 2200 and 1 <= m <= 12 else None
    except (ValueError, TypeError):
        return None


def _month_days(y, m):
    return calendar.monthrange(y, m)[1]


def identity(p):
    # Never strip underscores or coerce identifiers to numbers.
    return '|'.join(str(p.get(k, '')).strip() for k in ('supplier', 'sku', 'warehouse'))


def _median(values, default=0.0):
    return st.median(values) if values else default


def _event_adjustments(product, as_of):
    """Deduplicate events; remove rare document/customer-week spikes once.

    Only normalized anonymized sale events are accepted. Recurring large
    purchases in >=3 different months are not automatically removed.
    """
    events, seen = [], set()
    for e in product.get('transactions', []):
        try:
            d = date.fromisoformat(str(e['date'])[:10])
        except (ValueError, KeyError):
            continue
        qty = _finite_number(e.get('quantity'))
        key = str(e.get('event_id', ''))
        if not key or key in seen or d > as_of or qty is None or qty <= 0:
            continue
        seen.add(key)
        events.append({**e, 'date': d, 'quantity': qty})
    if len(events) < 5:
        return {}, []
    docs = defaultdict(list)
    customers = defaultdict(list)
    for e in events:
        docs[(e.get('document_id') or e['event_id'], e['date'].isoformat()[:7])].append(e)
        if e.get('customer_id'):
            customers[e['customer_id']].append(e)
    sizes = [sum(e['quantity'] for e in group) for group in docs.values()]
    med = _median(sizes)
    mad = _median([abs(x - med) for x in sizes])
    monthly = [_finite_number(x) for x in product.get('monthly_sales', {}).values()]
    typical = _median([x for x in monthly if x is not None and x > 0])
    threshold = max(8 * med, med + 8 * 1.4826 * mad, 2 * typical, 1.0)
    candidates = [('document', g) for g in docs.values() if sum(e['quantity'] for e in g) > threshold]
    for seq in customers.values():
        seq.sort(key=lambda e: e['date'])
        # Sliding, not calendar weeks: an event on Sunday can be paired with Monday.
        for start in range(len(seq)):
            group = [e for e in seq[start:] if (e['date'] - seq[start]['date']).days < 7]
            if len(group) >= 2 and sum(e['quantity'] for e in group) > threshold:
                candidates.append(('customer_7_days', group))
    removed, evidence, used = defaultdict(float), [], set()
    large_months = {e['date'].isoformat()[:7] for kind, g in candidates for e in g}
    if len(large_months) >= 3:
        return {}, []
    for kind, group in candidates:
        fresh = [e for e in group if e['event_id'] not in used]
        if not fresh:
            continue
        for e in fresh:
            used.add(e['event_id'])
            removed[e['date'].isoformat()[:7]] += e['quantity']
        evidence.append({'kind': kind, 'events': len(fresh), 'units': sum(e['quantity'] for e in fresh), 'threshold': threshold})
    return dict(removed), evidence


def _clean_monthly_sales(product, as_of):
    detected, _ = _event_adjustments(product, as_of)
    explicit = product.get('one_off_adjustments', {})
    rows, removals, details = [], {}, {}
    for period, raw in sorted(product.get('monthly_sales', {}).items()):
        parsed, value = _month(period), _finite_number(raw)
        if not parsed or parsed > (as_of.year, as_of.month) or value is None or value < 0:
            continue
        # Both fields can describe the same event; never subtract it twice.
        take = min(value, max(_finite_number(explicit.get(period), 0), detected.get(period, 0)))
        if take:
            removals[period] = take
            details[period] = {'source_adjustment_units': take}
        rows.append((period, value - take))
    # Monthly-only fallback: seasonal peers are checked before clipping.
    positives = [v for _, v in rows if v > 0]
    if len(positives) >= 6:
        cleaned = []
        for period, value in rows:
            peers = [v for p, v in rows if p != period and p[-2:] == period[-2:] and v > 0]
            others = [v for p, v in rows if p != period and v > 0]
            med = _median(others)
            mad = _median([abs(v - med) for v in others])
            recurring_season = bool(peers and _median(peers) >= value / 2)
            threshold = max(4 * med, med + 6 * 1.4826 * mad, 1)
            if value > threshold and not recurring_season:
                replacement = _median(peers, med)
                take = max(0, value - replacement)
                removals[period] = removals.get(period, 0) + take
                details.setdefault(period, {})['spike_cap_units'] = take
                value = replacement
            cleaned.append((period, value))
        rows = cleaned
    return rows, removals, details


def _exposure(period, as_of):
    y, m = _month(period)
    return as_of.day if (y, m) == (as_of.year, as_of.month) else _month_days(y, m)


def _stockout_adjustments(adjusted, stockout_months, as_of):
    values, compensation = dict(adjusted), 0.0
    # Baselines are learned from uncensored observations only, including zero demand.
    rates = [(p, v / _exposure(p, as_of)) for p, v in adjusted if not stockout_months.get(p)]
    for p, raw_days in stockout_months.items():
        if p not in values or not _month(p) or _month(p) > (as_of.year, as_of.month):
            continue
        days = _finite_number(raw_days)
        exposure = _exposure(p, as_of)
        if days is None or days != int(days) or not 0 <= days <= exposure:
            continue
        comparable = [r for other, r in rates if other[-2:] == p[-2:]]
        available_days = exposure - days
        own_rate = values[p] / available_days if available_days >= 7 else None
        prior = _median(comparable, _median([r for _, r in rates]))
        rate = max(prior, own_rate or 0)
        lost = max(0, rate * days)
        values[p] += lost
        compensation += lost
    return sorted(values.items()), compensation


def forecast_demand(product, *, as_of, horizon_days, demand_multiplier=1.0, stockout_months=None):
    adjusted, removals, details = _clean_monthly_sales(product, as_of)
    adjusted, lost = _stockout_adjustments(adjusted, stockout_months or {}, as_of)
    # Normalize month-to-date exactly once, after compensation.
    rates = [(p, v / _exposure(p, as_of)) for p, v in adjusted][-36:]
    yearly = defaultdict(list)
    for p, rate in rates:
        if p != as_of.strftime('%Y-%m'):
            yearly[p[:4]].append(rate)
    annual = {y: st.mean(v) for y, v in yearly.items() if len(v) >= 9 and sum(v) > 0}
    groups = defaultdict(list)
    for p, rate in rates:
        if p[:4] in annual and p != as_of.strftime('%Y-%m'):
            groups[int(p[-2:])].append(rate / annual[p[:4]])
    supplied = product.get('seasonality_factors') or product.get('category_seasonality') or {}
    factors = {}
    for m in range(1, 13):
        external = _finite_number(supplied.get(str(m), supplied.get(m)))
        factors[m] = external if external is not None and external >= 0 else _median(groups[m], 1.0)
    mean_factor = st.mean(factors.values()) or 1.0
    factors = {m: max(0.01, v / mean_factor) for m, v in factors.items()}
    deseason = [(p, rate / factors[int(p[-2:])]) for p, rate in rates]
    recent = [v for _, v in deseason[-6:]]
    base = sum(v * (i + 1) for i, v in enumerate(recent)) / sum(range(1, len(recent) + 1)) if recent else 0.0
    old = [v for _, v in deseason[-12:-6]]
    trend = (st.mean(recent) / st.mean(old)) ** (horizon_days / 180) if len(old) >= 3 and st.mean(old) > 0 and recent else 1.0
    trend = max(0.7, min(1.35, trend))
    supplied_growth = _finite_number(product.get('growth_factor'))
    # A partner forecast replaces the fitted growth adjustment, not multiplies it.
    growth = supplied_growth if supplied_growth is not None and supplied_growth > 0 else trend
    multiplier = _finite_number(demand_multiplier, 1.0)
    daily = []
    seasons = []
    for i in range(1, horizon_days + 1):
        future = as_of + timedelta(days=i)
        seasonal = factors[future.month]
        # Factor applies even to a horizon entirely within one calendar month.
        g = growth if supplied_growth is not None else growth ** (i / max(1, horizon_days))
        daily.append(max(0, base * seasonal * g * multiplier))
        seasons.append(seasonal)
    _, evidence = _event_adjustments(product, as_of)
    return {
        'forecast_units': round(sum(daily), 6), 'daily_forecast': daily,
        'seasonality_factor': round(st.mean(seasons), 6) if seasons else 1.0,
        'growth_factor': round(growth, 6), 'growth_source': 'provided_replace' if supplied_growth is not None else 'deseasonalized_trend',
        'anomaly_removed_units': round(sum(removals.values()), 6),
        'stockout_compensation_units': round(lost, 6),
        'monthly_adjustments': removals, 'monthly_adjustment_details': details,
        'event_evidence': evidence, 'adjusted_history': dict(adjusted),
        'history_observations': len(rates), 'base_daily': base,
        'seasonality_factors': factors,
    }


def validate_scenario(scenario, suppliers):
    source = scenario if isinstance(scenario, dict) else {}
    out, warnings = {}, []
    out['supplier'] = str(source.get('supplier', 'all'))
    if out['supplier'] != 'all' and out['supplier'] not in suppliers:
        out['supplier'] = 'all'
        warnings.append('Поставщик не найден, показаны все поставщики.')
    for key, default, low, high in [('horizon_days', 30, 1, 180), ('review_days', 30, 1, 180), ('stockout_days', 0, 0, 31)]:
        value = _finite_number(source.get(key), default)
        if value != int(value) or not low <= value <= high:
            warnings.append(f'{key}: значение ограничено допустимым диапазоном.')
        out[key] = max(low, min(high, int(value)))
    for key, default, low, high in [('demand_multiplier', 1, .5, 3), ('stock_delta', 0, -100000, 100000), ('inbound_delta', 0, -100000, 100000)]:
        value = _finite_number(source.get(key), default)
        out[key] = max(low, min(high, value))
    for key in ('selected_sku', 'category', 'warehouse'):
        out[key] = str(source.get(key, '') or '').strip()
    out['stockouts_by_sku'] = source.get('stockouts_by_sku') if isinstance(source.get('stockouts_by_sku'), dict) else {}
    out['policy_confirmed'] = source.get('policy_confirmed') is True
    return out, warnings


def _item_stockout_map(product, scenario, as_of):
    result = dict(product.get('stockout_months') or {})
    uploaded = scenario.get('stockouts_by_sku', {}).get(identity(product), scenario.get('stockouts_by_sku', {}).get(product['sku'], {}))
    if isinstance(uploaded, dict):
        result.update(uploaded)
    if scenario.get('selected_sku') in (product['sku'], identity(product)) and scenario.get('stockout_days', 0):
        result[as_of.strftime('%Y-%m')] = min(as_of.day, scenario['stockout_days'])
    return result


def _rounded_order(shortfall, multiple, minimum=0):
    if shortfall <= 0:
        return 0.0
    step = Decimal(str(multiple))
    if step <= 0:
        raise ValueError('Кратность должна быть положительной.')
    qty = max(Decimal(str(shortfall)), Decimal(str(minimum)))
    return float((qty / step).to_integral_value(rounding=ROUND_CEILING) * step)


def calculate_recommendations(catalog, scenario=None):
    products = catalog.get('products') or []
    suppliers = {str(p.get('supplier', '')) for p in products}
    params, warnings = validate_scenario(scenario, suppliers)
    as_of = date.fromisoformat(str(catalog['as_of_date']))
    selected = params['selected_sku']
    matches = [p for p in products if selected in (p.get('sku'), identity(p))]
    if selected and len(matches) != 1:
        params['selected_sku'] = ''
        warnings.append('SKU сценария не найден или неоднозначен; изменения не применены.')
    real = catalog.get('data_mode') not in ('synthetic', 'test')
    evaluated, incomplete = [], []
    no_demand = 0
    for p in products:
        if params['supplier'] != 'all' and p.get('supplier') != params['supplier']:
            continue
        if params['category'] and p.get('category') != params['category']:
            continue
        if params['warehouse'] and p.get('warehouse') != params['warehouse']:
            continue
        missing = []
        stock = _finite_number(p.get('current_stock'))
        if not p.get('stock_known') or stock is None or stock < 0:
            missing.append('Остаток')
        if not p.get('monthly_sales'):
            missing.append('История продаж')
        if not p.get('inbound_known'):
            missing.append('Товары в пути')
        if missing:
            incomplete.append({'sku': p['sku'], 'item_id': identity(p), 'supplier': p.get('supplier'), 'name': p.get('name'), 'missing': missing[0], 'issues': missing})
            continue
        policy = catalog.get('category_policies', {}).get(f"{p.get('supplier')}|{p.get('category')}", {})
        lead = int(_finite_number(p.get('lead_days'), params['horizon_days']))
        review = int(_finite_number(policy.get('review_days'), params['review_days']))
        if not 1 <= lead <= 180 or not 1 <= review <= 180:
            raise ValueError('Недопустимые сроки поставки или проверки.')
        horizon = lead + review
        buffer_pct = _finite_number(policy.get('buffer_pct'), .15)
        if not 0 <= buffer_pct <= 1:
            raise ValueError('Недопустимая политика страхового запаса.')
        blockers = list(p.get('approval_blockers', []))
        notes = list(p.get('data_warnings', []))
        if real:
            if p.get('stock_snapshot') != as_of.isoformat():
                blockers.append('Нет подтверждённого актуального остатка на дату расчёта.')
            if not p.get('category_known'):
                blockers.append('Категория не подтверждена.')
            if not (policy.get('confirmed') or params['policy_confirmed']):
                blockers.append('Политика категории и сроки требуют подтверждения менеджера.')
            if catalog.get('schema_version') != 2:
                blockers.append('Нужен повторный импорт: старый каталог потерял даты и единицы.')
        if not p.get('moq_known'):
            notes.append('Кратность/минимальная партия не подтверждены; расчёт без ограничения поставщика.')
        factor = _finite_number(p.get('units_per_purchase'), 1)
        if factor <= 0:
            blockers.append('Неверный перевод закупочных единиц.')
            factor = 1
        if p.get('purchase_unit', p.get('unit')) != p.get('unit') and not p.get('conversion_confirmed'):
            blockers.append('Перевод закупочных и складских единиц не подтверждён.')
        forecast = forecast_demand(p, as_of=as_of, horizon_days=horizon, demand_multiplier=params['demand_multiplier'], stockout_months=_item_stockout_map(p, params, as_of))
        daily = forecast['daily_forecast']
        arrivals, undated, late = defaultdict(float), 0.0, 0.0
        shipments = p.get('inbound_shipments')
        if shipments is None:
            undated = max(0, _finite_number(p.get('in_transit'), 0))
        else:
            for shipment in shipments:
                qty = _finite_number(shipment.get('quantity'))
                if qty is None or qty < 0:
                    blockers.append('Некорректное количество товара в пути.')
                    continue
                if shipment.get('status', 'confirmed') != 'confirmed':
                    continue
                try:
                    offset = (date.fromisoformat(shipment['eta']) - as_of).days
                except (ValueError, KeyError, TypeError):
                    undated += qty
                    continue
                if offset <= 0:
                    undated += qty
                elif offset <= horizon:
                    arrivals[offset] += qty
                else:
                    late += qty
        if undated:
            blockers.append('Есть поставки без актуальной даты; не считаются доступными.')
        if params['selected_sku'] in (p['sku'], identity(p)):
            stock = max(0, stock + params['stock_delta'])
            if params['inbound_delta']:
                arrivals[lead] = max(0, arrivals[lead] + params['inbound_delta'])
                notes.append('What-if: изменение поставки применено к дате нового поступления.')
        # Lost sales before new delivery are a risk, not an artificial backlog.
        balance, lost_before, first_deficit = stock, 0.0, None
        for day in range(1, lead):
            balance += arrivals.get(day, 0)
            gap = max(0, daily[day-1] - balance)
            if gap and first_deficit is None:
                first_deficit = (as_of + timedelta(days=day)).isoformat()
            lost_before += gap
            balance = max(0, balance - daily[day-1])
        at_arrival = balance
        deficit_after = 0.0
        for day in range(lead, horizon + 1):
            balance += arrivals.get(day, 0) - daily[day-1]
            deficit_after = max(deficit_after, -balance)
        safety = sum(daily[lead-1:]) * buffer_pct
        shortfall = max(0, deficit_after, safety - balance)
        multiple = _finite_number(p.get('order_multiple'), _finite_number(p.get('moq'), 1))
        minimum = _finite_number(p.get('min_order'), 0)
        if multiple <= 0 or minimum < 0:
            blockers.append('Некорректные ограничения поставщика.')
            multiple, minimum = 1, 0
        purchase_qty = _rounded_order(shortfall / factor, multiple, minimum)
        order_qty = purchase_qty * factor
        if not sum(daily):
            no_demand += 1
        urgency = 'Срочно' if lost_before > 0 else 'Высокий риск' if deficit_after > 0 else 'Планово'
        trace = {
            'algorithm_version': VERSION, 'lead_days': lead, 'review_days': review,
            'horizon_days': horizon, 'forecast': forecast['forecast_units'],
            'stock_on_snapshot': stock, 'stock_before_new_delivery': at_arrival,
            'eligible_inbound': sum(arrivals.values()), 'late_inbound': late, 'undated_inbound': undated,
            'lost_before_delivery': lost_before, 'first_deficit_date': first_deficit,
            'post_arrival_deficit': max(0, deficit_after), 'end_balance_without_order': balance,
            'safety_buffer': safety, 'buffer_pct': buffer_pct, 'shortfall_stock_units': shortfall,
            'units_per_purchase': factor, 'minimum_purchase_qty': minimum, 'order_multiple': multiple,
            'purchase_qty': purchase_qty, 'order_stock_units': order_qty,
            'growth_source': forecast['growth_source'], 'policy': policy,
        }
        explanation = (f"Период {lead}+{review} дней; спрос {sum(daily):.2f} {p.get('unit', '')}. "
                       f"Остаток на срезе {stock:g}; поступления в пределах горизонта {sum(arrivals.values()):g}. "
                       f"Остаток без нового заказа в конце {balance:.2f}; страховой запас {safety:.2f}. "
                       f"Дефицит после даты нового поступления {max(0, deficit_after):.2f}; "
                       f"потребность max(0, дефицит, страховой запас − конечный остаток) = {shortfall:.2f}. "
                       f"Закупка {purchase_qty:g} {p.get('purchase_unit', p.get('unit', ''))}; "
                       f"минимум {minimum:g}, кратность {multiple:g}, коэффициент перевода {factor:g}. "
                       f"Потенциально потеряно до новой поставки {lost_before:.2f}; это не включено в заказ как фиктивный долг.")
        row = {
            **{k: p.get(k, '') for k in ('sku', 'supplier', 'supplier_sku', 'name', 'category', 'unit', 'warehouse', 'stock_snapshot', 'stock_basis')},
            'item_id': identity(p), 'current_stock': stock, 'in_transit': sum(arrivals.values()),
            'forecast_units': forecast['forecast_units'], 'safety_buffer': safety,
            'moq': multiple, 'moq_known': p.get('moq_known', False), 'recommended_qty': order_qty,
            'purchase_qty': purchase_qty, 'purchase_unit': p.get('purchase_unit', p.get('unit', '')),
            'cover_days': round(stock / (sum(daily) / horizon), 1) if sum(daily) else None,
            'urgency': urgency, 'seasonality_factor': forecast['seasonality_factor'], 'growth_factor': forecast['growth_factor'],
            'anomaly_removed_units': forecast['anomaly_removed_units'], 'stockout_compensation_units': forecast['stockout_compensation_units'],
            'anomaly_adjustments': [{'month': period, 'units': units, 'reason': key} for period, fields in forecast['monthly_adjustment_details'].items() for key, units in fields.items()],
            'explanation': explanation, 'trace': trace, 'approval_blockers': sorted(set(blockers)), 'notes': notes,
            'ready_for_approval': not blockers, 'adjusted_history': forecast['adjusted_history'],
        }
        evaluated.append(row)
    rows = sorted([r for r in evaluated if r['recommended_qty'] > 0], key=lambda r: (r['supplier'], URGENCY_ORDER[r['urgency']], r['sku']))
    units = defaultdict(float)
    for r in rows:
        units[r['unit']] += r['recommended_qty']
    grouped = {s: {'positions': sum(r['supplier'] == s for r in rows), 'urgent': sum(r['supplier'] == s and r['urgency'] == 'Срочно' for r in rows)} for s in sorted(suppliers)}
    if real:
        warnings.append('Рекомендации с ограничениями нельзя утверждать до исправления источников. Снимки не являются данными реального времени.')
    return {'as_of_date': as_of.isoformat(), 'scenario': params, 'warnings': warnings,
            'recommendations': rows, 'evaluated': evaluated, 'incomplete': incomplete, 'by_supplier': grouped,
            'metrics': {'catalog_products': len(products), 'recommendation_positions': len(rows), 'urgent_positions': sum(r['urgency'] == 'Срочно' for r in rows),
                        'high_risk_positions': sum(r['urgency'] == 'Высокий риск' for r in rows), 'incomplete_products': len(incomplete), 'no_demand_products': no_demand,
                        'blocked_positions': sum(not r['ready_for_approval'] for r in rows), 'units_by_unit': dict(units),
                        'recommended_units': sum(units.values()) if len(units) <= 1 else None},
            'data_limits': catalog.get('data_limits', []), 'data_mode': catalog.get('data_mode', 'unverified'), 'algorithm_version': VERSION}
