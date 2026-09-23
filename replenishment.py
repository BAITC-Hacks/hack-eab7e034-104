"""Deterministic inventory forecasting and replenishment calculations."""

from __future__ import annotations

import calendar
import math
import statistics
from datetime import date
from typing import Any


URGENCY_ORDER = {"Срочно": 0, "Высокий риск": 1, "Планово": 2}


def _finite_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _month(value: str) -> tuple[int, int] | None:
    try:
        year, month = (int(part) for part in value.split("-", 1))
        if 1 <= month <= 12:
            return year, month
    except (ValueError, AttributeError):
        pass
    return None


def _month_days(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _periods_between(start: date, days: int):
    cursor = start
    remaining = max(0, days)
    while remaining:
        days_in_month = _month_days(cursor.year, cursor.month)
        available = days_in_month - cursor.day + 1
        span = min(remaining, available)
        yield cursor.year, cursor.month, span, days_in_month
        remaining -= span
        cursor = date(*_next_month(cursor.year, cursor.month), 1)


def _linear_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    weights = list(range(1, len(values) + 1))
    return sum(value * weight for value, weight in zip(values, weights)) / sum(weights)


def _seasonality(adjusted: list[tuple[str, float]], year: int, month: int) -> float:
    positive = [value for _period, value in adjusted if value > 0]
    if len(positive) < 12:
        return 1.0
    month_values = [value for period, value in adjusted if (_month(period) or (0, 0))[1] == month and value > 0]
    if len(month_values) < 2:
        return 1.0
    overall = statistics.median(positive)
    seasonal = statistics.median(month_values)
    if overall <= 0:
        return 1.0
    return max(0.25, min(3.0, seasonal / overall))


def _growth(adjusted: list[tuple[str, float]], horizon_days: int) -> float:
    values = [value for _period, value in adjusted]
    if len(values) < 12:
        return 1.0
    recent = values[-6:]
    previous = values[-12:-6]
    recent_mean = statistics.mean(recent)
    previous_mean = statistics.mean(previous)
    if recent_mean <= 0 or previous_mean <= 0:
        return 1.0
    half_year_ratio = recent_mean / previous_mean
    # Dampen the latest six-month change and scale it to the planning window.
    factor = half_year_ratio ** (max(0, horizon_days) / 180.0)
    return max(0.7, min(1.35, factor))


def _clean_monthly_sales(product: dict, as_of: date) -> tuple[list[tuple[str, float]], dict[str, float]]:
    raw_months = product.get("monthly_sales") or {}
    ordered = sorted(
        (period, max(0.0, _finite_number(value)))
        for period, value in raw_months.items()
        if _month(period) and _month(period) <= (as_of.year, as_of.month)
    )
    if not ordered:
        return [], {}

    removals: dict[str, float] = {}
    event_adjustments = product.get("one_off_adjustments") or {}
    post_event: list[tuple[str, float]] = []
    for period, value in ordered:
        removed = min(value, max(0.0, _finite_number(event_adjustments.get(period))))
        remaining = max(0.0, value - removed)
        if removed > 0:
            removals[period] = removed
        year, month = _month(period)
        if year == as_of.year and month == as_of.month:
            # The supplied September reports are dated Sep 22, so their current
            # month is treated as month-to-date and projected to a full month.
            covered_days = max(1, min(as_of.day, _month_days(year, month)))
            remaining *= _month_days(year, month) / covered_days
            if removed > 0:
                removals[period] = min(value, removed * _month_days(year, month) / covered_days)
        post_event.append((period, remaining))

    positive = [value for _period, value in post_event if value > 0]
    if len(positive) >= 6:
        overall_median = statistics.median(positive)
        caps: list[tuple[str, float]] = []
        for period, value in post_event:
            year, month = _month(period)
            seasonal_reference = [v for other_period, v in post_event if other_period != period and (_month(other_period) or (0, 0))[1] == month and v > 0]
            if len(seasonal_reference) >= 2:
                cap = min(statistics.median(seasonal_reference) * 3.0, overall_median * 4.0)
            else:
                # With fewer than two comparable seasons, use a tighter robust
                # cap so a single invoice spike cannot distort the baseline.
                cap = overall_median * 2.0
            cap = max(1.0, cap)
            if value > cap:
                removals[period] = removals.get(period, 0.0) + (value - cap)
                caps.append((period, cap))
            else:
                caps.append((period, value))
        post_event = caps
    return post_event, removals


def _stockout_adjustments(
    adjusted: list[tuple[str, float]], stockout_months: dict[str, int], as_of: date
) -> tuple[list[tuple[str, float]], float]:
    if not adjusted or not stockout_months:
        return adjusted, 0.0
    complete_history = [(period, value) for period, value in adjusted if period != f"{as_of.year:04d}-{as_of.month:02d}"]
    baseline = [value / 30.0 for _period, value in complete_history[-12:] if value > 0]
    daily_rate = statistics.median(baseline) if baseline else 0.0
    if daily_rate <= 0:
        return adjusted, 0.0
    values = dict(adjusted)
    compensated = 0.0
    for period, raw_days in stockout_months.items():
        parsed = _month(period)
        if not parsed or raw_days <= 0:
            continue
        year, month = parsed
        days = min(max(0, int(raw_days)), _month_days(year, month))
        estimated_lost = daily_rate * days
        values[period] = max(0.0, _finite_number(values.get(period))) + estimated_lost
        compensated += estimated_lost
    return sorted(values.items()), compensated


def forecast_demand(
    product: dict,
    *,
    as_of: date,
    horizon_days: int,
    demand_multiplier: float = 1.0,
    stockout_months: dict[str, int] | None = None,
) -> dict:
    adjusted, removals = _clean_monthly_sales(product, as_of)
    adjusted, stockout_compensation = _stockout_adjustments(adjusted, stockout_months or {}, as_of)
    history_values = [value for _period, value in adjusted]
    usable = history_values[-24:]
    if not usable or sum(usable) <= 0:
        return {
            "forecast_units": 0.0,
            "seasonality_factor": 1.0,
            "growth_factor": 1.0,
            "anomaly_removed_units": round(sum(removals.values()), 3),
            "stockout_compensation_units": round(stockout_compensation, 3),
            "monthly_adjustments": removals,
        }
    base_month = _linear_mean(usable)
    growth_factor = _growth(adjusted, horizon_days)
    multiplier = max(0.5, min(3.0, _finite_number(demand_multiplier, 1.0)))
    future_start = as_of.fromordinal(as_of.toordinal() + 1)
    demand = 0.0
    weighted_season = 0.0
    weight_days = 0
    day_offset = 0
    for year, month, span, days_in_month in _periods_between(future_start, horizon_days):
        seasonal_factor = _seasonality(adjusted, year, month)
        progress_factor = growth_factor ** (day_offset / max(1, horizon_days))
        period_demand = base_month * seasonal_factor * progress_factor * span / days_in_month
        demand += period_demand
        weighted_season += seasonal_factor * span
        weight_days += span
        day_offset += span
    return {
        "forecast_units": round(max(0.0, demand * multiplier), 3),
        "seasonality_factor": round(weighted_season / max(1, weight_days), 3),
        "growth_factor": round(growth_factor, 3),
        "anomaly_removed_units": round(sum(removals.values()), 3),
        "stockout_compensation_units": round(stockout_compensation * multiplier, 3),
        "monthly_adjustments": removals,
    }


def validate_scenario(scenario: dict | None, suppliers: set[str]) -> tuple[dict, list[str]]:
    source = scenario if isinstance(scenario, dict) else {}
    errors = []
    supplier = source.get("supplier", "all")
    if supplier != "all" and supplier not in suppliers:
        errors.append("Поставщик не найден, показаны все поставщики.")
        supplier = "all"
    try:
        horizon_days = int(source.get("horizon_days", 30))
    except (TypeError, ValueError):
        horizon_days = 30
        errors.append("Срок поставки должен быть целым числом дней. Использовано 30.")
    if not 1 <= horizon_days <= 180:
        horizon_days = max(1, min(180, horizon_days))
        errors.append("Срок поставки ограничен диапазоном 1–180 дней.")
    demand_multiplier = _finite_number(source.get("demand_multiplier", 1.0), 1.0)
    if not 0.5 <= demand_multiplier <= 3.0:
        demand_multiplier = max(0.5, min(3.0, demand_multiplier))
        errors.append("Изменение спроса ограничено диапазоном от −50% до +200%.")
    selected_sku = str(source.get("selected_sku", "") or "").strip()
    stock_delta = _finite_number(source.get("stock_delta", 0.0))
    inbound_delta = _finite_number(source.get("inbound_delta", 0.0))
    stockout_days = _finite_number(source.get("stockout_days", 0.0))
    if abs(stock_delta) > 100000 or abs(inbound_delta) > 100000:
        errors.append("Изменение остатка ограничено ±100 000 единиц.")
        stock_delta = max(-100000, min(100000, stock_delta))
        inbound_delta = max(-100000, min(100000, inbound_delta))
    if not 0 <= stockout_days <= 31:
        stockout_days = max(0, min(31, stockout_days))
        errors.append("Число stockout-дней ограничено диапазоном 0–31.")
    stockouts = source.get("stockouts_by_sku") or {}
    if not isinstance(stockouts, dict):
        stockouts = {}
        errors.append("Файл stockout не разобран, расчёт продолжен без его строк.")
    return {
        "supplier": supplier,
        "horizon_days": horizon_days,
        "demand_multiplier": demand_multiplier,
        "selected_sku": selected_sku,
        "stock_delta": stock_delta,
        "inbound_delta": inbound_delta,
        "stockout_days": int(stockout_days),
        "stockouts_by_sku": stockouts,
    }, errors


def _item_stockout_map(product: dict, scenario: dict, as_of: date) -> dict[str, int]:
    stockout_map: dict[str, int] = {}
    uploaded = scenario.get("stockouts_by_sku", {}).get(product["sku"], {})
    if isinstance(uploaded, dict):
        for period, days in uploaded.items():
            try:
                parsed = _month(period)
                count = int(days)
                if parsed and 0 <= count <= _month_days(*parsed):
                    stockout_map[period] = count
            except (TypeError, ValueError):
                continue
    if scenario.get("selected_sku") == product["sku"] and scenario.get("stockout_days", 0) > 0:
        period = f"{as_of.year:04d}-{as_of.month:02d}"
        stockout_map[period] = max(stockout_map.get(period, 0), scenario["stockout_days"])
    return stockout_map


def _rounded_order(shortfall: float, moq: float) -> float:
    if shortfall <= 0:
        return 0.0
    lot = max(1.0, moq)
    return math.ceil((shortfall - 1e-9) / lot) * lot


def calculate_recommendations(catalog: dict, scenario: dict | None = None) -> dict:
    """Calculate and validate purchase suggestions using uploaded source fields."""
    products = catalog.get("products") or []
    suppliers = {str(item.get("supplier", "")) for item in products if item.get("supplier")}
    params, warnings = validate_scenario(scenario, suppliers)
    known_skus = {str(item.get("sku", "")) for item in products}
    if params["selected_sku"] and params["selected_sku"] not in known_skus:
        params["selected_sku"] = ""
        params["stock_delta"] = 0.0
        params["inbound_delta"] = 0.0
        params["stockout_days"] = 0
        warnings.append("SKU сценария не найден, изменения остатка и stockout не применены.")
    elif params["stockout_days"] > 0 and not params["selected_sku"]:
        params["stockout_days"] = 0
        warnings.append("Для симуляции stockout выберите конкретный SKU.")
    try:
        as_of = date.fromisoformat(str(catalog.get("as_of_date", "2026-09-22")))
    except ValueError:
        as_of = date(2026, 9, 22)
        warnings.append("Дата среза не разобрана, использовано 22 сентября 2026 года.")

    recommendations = []
    incomplete = []
    skipped = 0
    for product in products:
        supplier = str(product.get("supplier", ""))
        if params["supplier"] != "all" and supplier != params["supplier"]:
            continue
        sku = str(product.get("sku", ""))
        if not product.get("stock_known", False):
            incomplete.append({"sku": sku, "supplier": supplier, "name": product.get("name", ""), "missing": "Остаток"})
            continue
        if not product.get("monthly_sales"):
            incomplete.append({"sku": sku, "supplier": supplier, "name": product.get("name", ""), "missing": "История продаж"})
            continue
        if not product.get("inbound_known", False):
            incomplete.append({"sku": sku, "supplier": supplier, "name": product.get("name", ""), "missing": "Товары в пути"})
            continue

        forecast = forecast_demand(
            product,
            as_of=as_of,
            horizon_days=params["horizon_days"],
            demand_multiplier=params["demand_multiplier"],
            stockout_months=_item_stockout_map(product, params, as_of),
        )
        if forecast["forecast_units"] <= 0:
            skipped += 1
            continue
        stock = max(0.0, _finite_number(product.get("current_stock")))
        inbound = max(0.0, _finite_number(product.get("in_transit")))
        if params["selected_sku"] == sku:
            stock = max(0.0, stock + params["stock_delta"])
            inbound = max(0.0, inbound + params["inbound_delta"])
        buffer_units = forecast["forecast_units"] * 0.15
        target = forecast["forecast_units"] + buffer_units
        shortfall = max(0.0, target - stock - inbound)
        moq = max(1.0, _finite_number(product.get("moq", 1.0), 1.0))
        order_qty = _rounded_order(shortfall, moq)
        if order_qty <= 0:
            continue

        daily_rate = forecast["forecast_units"] / max(1, params["horizon_days"])
        cover_days = (stock + inbound) / daily_rate if daily_rate > 0 else math.inf
        if cover_days < params["horizon_days"] * 0.6:
            urgency = "Срочно"
        elif cover_days < params["horizon_days"]:
            urgency = "Высокий риск"
        else:
            urgency = "Планово"
        explanations = [
            f"прогноз на {params['horizon_days']} дн. — {forecast['forecast_units']:g} {product.get('unit', 'шт.')}",
            f"доступно {stock:g}, в пути {inbound:g}",
        ]
        if forecast["seasonality_factor"] != 1.0:
            explanations.append(f"сезонный коэффициент {forecast['seasonality_factor']:g}")
        if forecast["growth_factor"] != 1.0:
            explanations.append(f"тренд на горизонт {forecast['growth_factor']:.2f}×")
        if forecast["anomaly_removed_units"] > 0:
            explanations.append(f"из регулярного спроса снято {forecast['anomaly_removed_units']:g} выбросов")
        if forecast["stockout_compensation_units"] > 0:
            explanations.append(f"stockout-поправка +{forecast['stockout_compensation_units']:g}")
        if not product.get("moq_known", False):
            explanations.append("MOQ в выгрузке отсутствует, применена 1 ед.")
        if not product.get("category_known", False):
            explanations.append("категория не передана поставщиком")
        recommendations.append({
            "sku": sku,
            "supplier": supplier,
            "supplier_sku": product.get("supplier_sku", ""),
            "name": product.get("name", ""),
            "category": product.get("category", "Без категории"),
            "unit": product.get("unit", "шт."),
            "current_stock": round(stock, 3),
            "in_transit": round(inbound, 3),
            "forecast_units": forecast["forecast_units"],
            "safety_buffer": round(buffer_units, 3),
            "moq": moq,
            "recommended_qty": round(order_qty, 3),
            "cover_days": round(cover_days, 1) if math.isfinite(cover_days) else None,
            "urgency": urgency,
            "seasonality_factor": forecast["seasonality_factor"],
            "growth_factor": forecast["growth_factor"],
            "anomaly_removed_units": forecast["anomaly_removed_units"],
            "stockout_compensation_units": forecast["stockout_compensation_units"],
            "stock_basis": product.get("stock_basis", ""),
            "stock_snapshot": product.get("stock_snapshot", ""),
            "explanation": "; ".join(explanations) + f"; округлено по MOQ {moq:g}.",
            "moq_known": bool(product.get("moq_known", False)),
        })

    recommendations.sort(key=lambda row: (URGENCY_ORDER.get(row["urgency"], 9), -row["recommended_qty"], row["supplier"], row["sku"]))
    by_supplier = {}
    for supplier in sorted(suppliers):
        rows = [row for row in recommendations if row["supplier"] == supplier]
        by_supplier[supplier] = {
            "positions": len(rows),
            "urgent": sum(row["urgency"] == "Срочно" for row in rows),
            "high_risk": sum(row["urgency"] == "Высокий риск" for row in rows),
            "units": round(sum(row["recommended_qty"] for row in rows), 3),
        }
    return {
        "as_of_date": as_of.isoformat(),
        "scenario": params,
        "warnings": warnings,
        "recommendations": recommendations,
        "incomplete": incomplete,
        "by_supplier": by_supplier,
        "metrics": {
            "catalog_products": len(products),
            "recommendation_positions": len(recommendations),
            "urgent_positions": sum(row["urgency"] == "Срочно" for row in recommendations),
            "high_risk_positions": sum(row["urgency"] == "Высокий риск" for row in recommendations),
            "recommended_units": round(sum(row["recommended_qty"] for row in recommendations), 3),
            "incomplete_products": len(incomplete),
            "no_demand_products": skipped,
        },
        "data_limits": catalog.get("data_limits", []),
    }
