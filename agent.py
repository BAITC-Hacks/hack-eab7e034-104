"""Tool-using procurement assistant with a deterministic offline mode."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from replenishment import _item_stockout_map, calculate_recommendations, forecast_demand


TOOL_DEFINITIONS = [
    {
        "name": "summarize_replenishment_plan",
        "description": "Calculate and summarize the current supplier replenishment plan, including urgent items.",
        "properties": {
            "supplier": {"type": "string", "enum": ["all", "Systeme Electric", "IEK"]},
            "horizon_days": {"type": "integer", "minimum": 1, "maximum": 180},
            "demand_multiplier": {"type": "number", "minimum": 0.5, "maximum": 3.0},
        },
        "required": ["supplier", "horizon_days", "demand_multiplier"],
    },
    {
        "name": "explain_sku",
        "description": "Explain the deterministic replenishment calculation for one SKU.",
        "properties": {
            "sku": {"type": "string"},
            "supplier": {"type": "string", "enum": ["all", "Systeme Electric", "IEK"]},
            "horizon_days": {"type": "integer", "minimum": 1, "maximum": 180},
            "demand_multiplier": {"type": "number", "minimum": 0.5, "maximum": 3.0},
        },
        "required": ["sku", "supplier", "horizon_days", "demand_multiplier"],
    },
    {
        "name": "review_sales_anomalies",
        "description": "List monthly sales adjustments removed from regular demand by the source adjustment or robust spike cap.",
        "properties": {
            "supplier": {"type": "string", "enum": ["all", "Systeme Electric", "IEK"]},
            "horizon_days": {"type": "integer", "minimum": 1, "maximum": 180},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["supplier", "horizon_days", "limit"],
    },
    {
        "name": "audit_replenishment_data",
        "description": "Check missing source fields and stock snapshot freshness; month-only dates must be reported as approximate.",
        "properties": {
            "supplier": {"type": "string", "enum": ["all", "Systeme Electric", "IEK"]},
            "freshness_threshold_days": {"type": "integer", "minimum": 1, "maximum": 365},
        },
        "required": ["supplier", "freshness_threshold_days"],
    },
    {
        "name": "compare_replenishment_scenarios",
        "description": "Calculate two plans using the deterministic engine and report SKU and total-quantity changes.",
        "properties": {
            "supplier": {"type": "string", "enum": ["all", "Systeme Electric", "IEK"]},
            "baseline_horizon_days": {"type": "integer", "minimum": 1, "maximum": 180},
            "scenario_horizon_days": {"type": "integer", "minimum": 1, "maximum": 180},
            "baseline_demand_multiplier": {"type": "number", "minimum": 0.5, "maximum": 3.0},
            "scenario_demand_multiplier": {"type": "number", "minimum": 0.5, "maximum": 3.0},
        },
        "required": ["supplier", "baseline_horizon_days", "scenario_horizon_days", "baseline_demand_multiplier", "scenario_demand_multiplier"],
    },
    {
        "name": "prepare_manual_approval_export",
        "description": "Calculate the supplier order draft and prepare a preview for a CSV that still requires human approval.",
        "properties": {
            "supplier": {"type": "string", "enum": ["all", "Systeme Electric", "IEK"]},
            "horizon_days": {"type": "integer", "minimum": 1, "maximum": 180},
            "demand_multiplier": {"type": "number", "minimum": 0.5, "maximum": 3.0},
        },
        "required": ["supplier", "horizon_days", "demand_multiplier"],
    },
]

TOOLS = [
    {
        "type": "function",
        "name": item["name"],
        "description": item["description"],
        "parameters": {
            "type": "object",
            "properties": item["properties"],
            "required": item["required"],
            "additionalProperties": False,
        },
        "strict": True,
    }
    for item in TOOL_DEFINITIONS
]
TOOL_NAMES = {item["name"] for item in TOOLS}


def load_local_env(root: Path) -> None:
    """Load missing keys from a local .env file without echoing their values."""
    env_path = root / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and value:
                os.environ.setdefault(key, value)
    except OSError:
        return


def _api_request(body: dict, api_key: str, timeout: int = 25) -> dict:
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _response_text(response: dict) -> str:
    parts = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text" and block.get("text"):
                parts.append(block["text"])
    return "\n".join(parts).strip()


def _bound_int(value: Any, default: int, low: int, high: int) -> tuple[int, str | None]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default, f"Не удалось разобрать число; использовано {default}."
    bounded = max(low, min(high, number))
    return bounded, f"Значение ограничено диапазоном {low}–{high}." if bounded != number else None


def _bound_float(value: Any, default: float, low: float, high: float) -> tuple[float, str | None]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default, "Не удалось разобрать процент; использовано исходное значение."
    if not (number == number and abs(number) != float("inf")):
        return default, "Некорректное число заменено исходным значением."
    bounded = max(low, min(high, number))
    return bounded, f"Значение ограничено диапазоном {low:g}–{high:g}." if bounded != number else None


def _safe_common(arguments: dict, catalog: dict, base_scenario: dict) -> tuple[dict, list[str]]:
    suppliers = {str(item.get("supplier", "")) for item in catalog.get("products", []) if item.get("supplier")}
    warnings: list[str] = []
    supplier = arguments.get("supplier", base_scenario.get("supplier", "all"))
    if supplier != "all" and supplier not in suppliers:
        supplier = "all"
        warnings.append("Поставщик не найден; включены все поставщики.")
    horizon, warning = _bound_int(arguments.get("horizon_days", base_scenario.get("horizon_days", 30)), 30, 1, 180)
    if warning:
        warnings.append(warning)
    multiplier, warning = _bound_float(arguments.get("demand_multiplier", base_scenario.get("demand_multiplier", 1.0)), 1.0, 0.5, 3.0)
    if warning:
        warnings.append(warning)
    return {**base_scenario, "supplier": supplier, "horizon_days": horizon, "demand_multiplier": multiplier}, warnings


def _date_from_catalog(catalog: dict) -> date:
    try:
        return date.fromisoformat(str(catalog.get("as_of_date", "")))
    except ValueError:
        return date(2026, 9, 22)


def _fmt(value: Any, digits: int = 1) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not (number == number and abs(number) != float("inf")):
        return "—"
    return f"{number:,.{digits}f}".replace(",", " ").replace(".", ",")


def _freshness_audit(catalog: dict, supplier: str, threshold_days: int, *, today: date | None = None) -> dict:
    today = today or date.today()
    selected = [item for item in catalog.get("products", []) if supplier == "all" or item.get("supplier") == supplier]
    groups: dict[str, dict] = {}
    details = []
    for item in selected:
        name = str(item.get("supplier", "Без поставщика"))
        group = groups.setdefault(name, {
            "products": 0, "missing_stock": 0, "missing_sales": 0, "missing_inbound": 0,
            "missing_snapshot_date": 0, "stale_exact_dates": 0, "stale_month_dates": 0,
            "uncertain_month_dates": 0, "fresh_dates": 0, "missing_category": 0, "missing_moq": 0,
        })
        group["products"] += 1
        group["missing_stock"] += int(not item.get("stock_known", False))
        group["missing_sales"] += int(not bool(item.get("monthly_sales")))
        group["missing_inbound"] += int(not item.get("inbound_known", False))
        group["missing_category"] += int(not item.get("category_known", False))
        group["missing_moq"] += int(not item.get("moq_known", False))
        snapshot = str(item.get("stock_snapshot", "")).strip()
        state = "fresh"
        age_text = ""
        if not snapshot:
            group["missing_snapshot_date"] += 1
            state = "no_date"
            age_text = "дата среза не указана"
        else:
            try:
                if re.fullmatch(r"20\d{2}-\d{2}", snapshot):
                    year, month = (int(part) for part in snapshot.split("-"))
                    first = date(year, month, 1)
                    if month == 12:
                        next_month = date(year + 1, 1, 1)
                    else:
                        next_month = date(year, month + 1, 1)
                    last = date.fromordinal(next_month.toordinal() - 1)
                    youngest_age = max(0, (today - last).days)
                    oldest_age = max(0, (today - first).days)
                    age_text = f"примерно {youngest_age}–{oldest_age} дн. (дата известна только до месяца)"
                    if youngest_age > threshold_days:
                        group["stale_month_dates"] += 1
                        state = "stale_month"
                    elif oldest_age > threshold_days:
                        group["uncertain_month_dates"] += 1
                        state = "uncertain_month"
                    else:
                        group["fresh_dates"] += 1
                        state = "month_date"
                else:
                    snapshot_day = date.fromisoformat(snapshot)
                    age = max(0, (today - snapshot_day).days)
                    age_text = f"{age} дн."
                    if age > threshold_days:
                        group["stale_exact_dates"] += 1
                        state = "stale_exact"
                    else:
                        group["fresh_dates"] += 1
            except (ValueError, OverflowError):
                group["missing_snapshot_date"] += 1
                state = "invalid_date"
                age_text = "не удалось разобрать дату"
        if state in ("stale_exact", "stale_month", "uncertain_month", "no_date", "invalid_date") and len(details) < 12:
            details.append({"sku": item.get("sku", ""), "supplier": name, "snapshot": snapshot, "age": age_text, "state": state})
    return {
        "as_of": today.isoformat(),
        "threshold_days": threshold_days,
        "groups": groups,
        "attention": details,
        "month_date_rule": "Для среза YYYY-MM возраст показан диапазоном от конца до начала месяца; если диапазон пересекает порог, свежесть помечена как неопределённая.",
    }


def _plan(catalog: dict, scenario: dict) -> dict:
    return calculate_recommendations(catalog, scenario)


def _anomaly_report(catalog: dict, scenario: dict, limit: int) -> dict:
    try:
        as_of = date.fromisoformat(str(catalog.get("as_of_date", "2026-09-22")))
    except ValueError:
        as_of = date(2026, 9, 22)
    found = []
    total_removed = 0.0
    event_counts = {"source": 0, "spike_cap": 0}
    removed_by_type = {"source": 0.0, "spike_cap": 0.0}
    for product in catalog.get("products", []):
        if scenario.get("supplier", "all") not in ("all", product.get("supplier")):
            continue
        if not product.get("monthly_sales"):
            continue
        forecast = forecast_demand(
            product,
            as_of=as_of,
            horizon_days=int(scenario.get("horizon_days", 30)),
            demand_multiplier=float(scenario.get("demand_multiplier", 1.0)),
            stockout_months={},
        )
        for period, split in forecast.get("monthly_adjustment_details", {}).items():
            for field, key, reason in (
                ("source_adjustment_units", "source", "разовая продажа отмечена в исходной выгрузке"),
                ("spike_cap_units", "spike_cap", "месячный пик ограничен устойчивым алгоритмом"),
            ):
                units = float(split.get(field, 0) or 0)
                if units <= 0:
                    continue
                event_counts[key] += 1
                removed_by_type[key] += units
                total_removed += units
                found.append({
                    "sku": product.get("sku", ""),
                    "supplier": product.get("supplier", ""),
                    "name": product.get("name", ""),
                    "month": period,
                    "units": round(units, 3),
                    "reason": reason,
                })
    found.sort(key=lambda item: (-item["units"], item["supplier"], item["sku"], item["month"]))
    return {
        "total_events": sum(event_counts.values()),
        "event_counts": event_counts,
        "removed_by_type": {key: round(value, 3) for key, value in removed_by_type.items()},
        "total_removed_units": round(total_removed, 3),
        "items": found[:limit],
    }


def _sku_report(catalog: dict, scenario: dict, sku: str) -> tuple[dict, list[str]]:
    product = next((item for item in catalog.get("products", []) if str(item.get("sku", "")) == sku), None)
    if product is None:
        return {}, [f"SKU {sku or 'не указан'} не найден в каталоге."]
    plan = _plan(catalog, {**scenario, "supplier": product.get("supplier", scenario.get("supplier", "all"))})
    row = next((item for item in plan["recommendations"] if item["sku"] == sku), None)
    facts = [f"{product.get('supplier')} · {product.get('name', sku)} · SKU {sku}."]
    if not product.get("stock_known", False):
        facts.append("Рекомендацию нельзя рассчитать: в источнике нет остатка.")
        return {"sku": sku, "row": None}, facts
    if not product.get("monthly_sales"):
        facts.append("Рекомендацию нельзя рассчитать: нет истории продаж.")
        return {"sku": sku, "row": None}, facts
    if not product.get("inbound_known", False):
        facts.append("Рекомендацию нельзя рассчитать: не указаны товары в пути.")
        return {"sku": sku, "row": None}, facts
    if row:
        facts.extend([
            f"Срочность: {row['urgency']}; прогноз на {scenario['horizon_days']} дн. — {_fmt(row['forecast_units'])} {row['unit']}.",
            f"Остаток {_fmt(row['current_stock'])} + в пути {_fmt(row['in_transit'])}; буфер {_fmt(row['safety_buffer'])}; MOQ {_fmt(row['moq'])}.",
            f"Предложено заказать {_fmt(row['recommended_qty'])} {row['unit']}. {row['explanation']}",
        ])
        anomalies = row.get("anomaly_adjustments") or []
        if anomalies:
            facts.extend(f"Поправка {item['month']}: снято {_fmt(item['units'])} — {item['reason']}." for item in anomalies[:4])
        return {"sku": sku, "row": row}, facts
    as_of = _date_from_catalog(catalog)
    forecast = forecast_demand(
        product,
        as_of=as_of,
        horizon_days=int(scenario.get("horizon_days", 30)),
        demand_multiplier=float(scenario.get("demand_multiplier", 1.0)),
        stockout_months=_item_stockout_map(product, scenario, as_of),
    )
    stock = max(0.0, float(product.get("current_stock", 0) or 0))
    inbound = max(0.0, float(product.get("in_transit", 0) or 0))
    if scenario.get("selected_sku") == sku:
        stock = max(0.0, stock + float(scenario.get("stock_delta", 0) or 0))
        inbound = max(0.0, inbound + float(scenario.get("inbound_delta", 0) or 0))
    target = forecast["forecast_units"] * 1.15
    facts.append(f"Заказ не предложен: прогноз {_fmt(forecast['forecast_units'])}, доступно вместе с поставками {_fmt(stock + inbound)}, цель с буфером 15% — {_fmt(target)}.")
    if forecast["forecast_units"] <= 0:
        facts.append("По доступной истории регулярный спрос не рассчитан.")
    elif target <= stock + inbound:
        facts.append("Текущего остатка и товаров в пути достаточно для горизонта сценария.")
    else:
        facts.append("Проверьте SKU: он не попал в активный список рекомендаций текущего фильтра поставщика.")
    return {"sku": sku, "row": None, "forecast": forecast}, facts


def _compare(catalog: dict, base_scenario: dict, args: dict) -> tuple[dict, dict, list[str]]:
    supplier = args.get("supplier", base_scenario.get("supplier", "all"))
    baseline_horizon, _ = _bound_int(args.get("baseline_horizon_days", base_scenario.get("horizon_days", 30)), 30, 1, 180)
    scenario_horizon, _ = _bound_int(args.get("scenario_horizon_days", base_scenario.get("horizon_days", 30)), 30, 1, 180)
    baseline_multiplier, _ = _bound_float(args.get("baseline_demand_multiplier", base_scenario.get("demand_multiplier", 1.0)), 1.0, 0.5, 3.0)
    scenario_multiplier, _ = _bound_float(args.get("scenario_demand_multiplier", base_scenario.get("demand_multiplier", 1.0)), 1.0, 0.5, 3.0)
    baseline_params = {**base_scenario, "supplier": supplier, "horizon_days": baseline_horizon, "demand_multiplier": baseline_multiplier}
    next_params = {**base_scenario, "supplier": supplier, "horizon_days": scenario_horizon, "demand_multiplier": scenario_multiplier}
    before = _plan(catalog, baseline_params)
    after = _plan(catalog, next_params)
    before_rows = {row["sku"]: row for row in before["recommendations"]}
    after_rows = {row["sku"]: row for row in after["recommendations"]}
    changes = []
    for sku in sorted(before_rows.keys() | after_rows.keys()):
        old = before_rows.get(sku)
        new = after_rows.get(sku)
        old_qty = old["recommended_qty"] if old else 0.0
        new_qty = new["recommended_qty"] if new else 0.0
        if old_qty != new_qty:
            row = new or old
            changes.append({"sku": sku, "name": row.get("name", ""), "supplier": row.get("supplier", ""), "before_qty": old_qty, "after_qty": new_qty, "delta_qty": round(new_qty - old_qty, 3)})
    changes.sort(key=lambda item: (-abs(item["delta_qty"]), item["supplier"], item["sku"]))
    details = {
        "baseline": {"scenario": before["scenario"], "metrics": before["metrics"]},
        "scenario": {"scenario": after["scenario"], "metrics": after["metrics"]},
        "changed_skus": len(changes),
        "top_changes": changes[:8],
    }
    facts = [
        f"Было: {_fmt(before['metrics']['recommended_units'])} ед. в {before['metrics']['recommendation_positions']} позициях; срочных {before['metrics']['urgent_positions']}.",
        f"Стало: {_fmt(after['metrics']['recommended_units'])} ед. в {after['metrics']['recommendation_positions']} позициях; срочных {after['metrics']['urgent_positions']}.",
        f"Изменение объёма: {_fmt(after['metrics']['recommended_units'] - before['metrics']['recommended_units'])} ед.; изменилось SKU: {len(changes)}.",
    ]
    facts.extend(f"{item['supplier']} · {item['sku']}: {_fmt(item['before_qty'])} → {_fmt(item['after_qty'])} ({_fmt(item['delta_qty'], 1)})." for item in changes[:5])
    return details, after, facts


def _execute_tool(name: str, arguments: dict, catalog: dict, base_scenario: dict) -> dict:
    if name not in TOOL_NAMES:
        raise ValueError("Unknown assistant tool")
    args = arguments if isinstance(arguments, dict) else {}
    warnings = []
    if name == "compare_replenishment_scenarios":
        details, result, facts = _compare(catalog, base_scenario, args)
        tool_label = name
    else:
        common_args = {**args, "horizon_days": args.get("horizon_days", base_scenario.get("horizon_days", 30)), "demand_multiplier": args.get("demand_multiplier", base_scenario.get("demand_multiplier", 1.0))}
        scenario, warnings = _safe_common(common_args, catalog, base_scenario)
        result = _plan(catalog, scenario)
        details = {}
        if name == "summarize_replenishment_plan":
            urgent = [row for row in result["recommendations"] if row["urgency"] in ("Срочно", "Высокий риск")]
            facts = [
                f"План: {result['metrics']['recommendation_positions']} позиций на {_fmt(result['metrics']['recommended_units'])} ед.",
                f"Срочно: {result['metrics']['urgent_positions']}; высокий риск: {result['metrics']['high_risk_positions']}; неполные данные: {result['metrics']['incomplete_products']} SKU.",
            ]
            facts.extend(f"{row['urgency']} · {row['supplier']} · {row['sku']} — {_fmt(row['recommended_qty'])} {row['unit']}; {row['explanation']}" for row in urgent[:6])
            details = {"urgent_items": urgent[:10], "by_supplier": result["by_supplier"]}
        elif name == "explain_sku":
            sku = re.sub(r"_+$", "", str(args.get("sku", "")).strip())
            if not sku:
                sku = str(scenario.get("selected_sku", "")).strip()
            details, facts = _sku_report(catalog, scenario, sku)
        elif name == "review_sales_anomalies":
            limit, warning = _bound_int(args.get("limit", 10), 10, 1, 20)
            if warning:
                warnings.append(warning)
            details = _anomaly_report(catalog, scenario, limit)
            facts = [
                f"Поправки из исходной выгрузки: {details['event_counts']['source']} случаев / {_fmt(details['removed_by_type']['source'])} ед.; автоматическое ограничение месячных пиков: {details['event_counts']['spike_cap']} случаев / {_fmt(details['removed_by_type']['spike_cap'])} ед.",
                f"Всего корректировок: {details['total_events']}; снято из регулярного спроса: {_fmt(details['total_removed_units'])} ед.",
            ]
            facts.extend(f"{item['supplier']} · {item['sku']} · {item['month']}: {_fmt(item['units'])} ед.; {item['reason']}." for item in details["items"][:8])
            facts.append("Client ID в источнике отсутствует; связать крупную продажу с одним клиентом нельзя.")
        elif name == "audit_replenishment_data":
            threshold, warning = _bound_int(args.get("freshness_threshold_days", 14), 14, 1, 365)
            if warning:
                warnings.append(warning)
            details = _freshness_audit(catalog, scenario["supplier"], threshold)
            facts = []
            for supplier, group in details["groups"].items():
                facts.append(f"{supplier}: SKU {group['products']}; нет остатка {group['missing_stock']}, истории {group['missing_sales']}, данных о поставках {group['missing_inbound']}; точная дата среза отсутствует у {group['missing_snapshot_date']}; старше {threshold} дн. по точной дате — {group['stale_exact_dates']}; дата только до месяца с неопределённой свежестью — {group['uncertain_month_dates']}; точно старые месячные срезы — {group['stale_month_dates']}.")
            facts.append(f"Порог {threshold} дней — демонстрационное допущение, не срок из ТЗ и не утверждённый SLA.")
            facts.extend(f"Проверить: {item['supplier']} · {item['sku']} · {item['age']}." for item in details["attention"][:5])
        elif name == "prepare_manual_approval_export":
            facts = [f"Черновик CSV подготовлен для {len(result['recommendations'])} строк по текущему плану.", "Экспорт включает только видимые строки с активными фильтрами. Перед передачей поставщику менеджер должен проверить и согласовать файл вручную."]
            details = {"export_rows": len(result["recommendations"]), "approval_required": True}
        else:
            raise ValueError("Tool has no executor")
        tool_label = name
    result["warnings"].extend(warnings)
    return {"result": result, "details": details, "facts": facts, "warnings": warnings, "tool": tool_label}


def _local_tool_selection(query: str, catalog: dict, base_scenario: dict, baseline_scenario: dict | None = None) -> tuple[str, dict]:
    text = query.lower()
    suppliers = {str(item.get("supplier", "")) for item in catalog.get("products", [])}
    supplier = base_scenario.get("supplier", "all")
    if re.search(r"\bie?k\b", text):
        supplier = "IEK"
    elif "systeme" in text or "систем" in text:
        supplier = "Systeme Electric"
    if supplier not in suppliers and supplier != "all":
        supplier = "all"
    current = {**base_scenario, "supplier": supplier}
    days = re.search(r"(\d{1,3})\s*(?:дн(?:ей|я)?|days?)", text)
    if days:
        current["horizon_days"] = max(1, min(180, int(days.group(1))))
    percent = re.search(r"(?:на\s*)?([+-]?\d{1,3})\s*%", text)
    if percent:
        current["demand_multiplier"] = max(0.5, min(3.0, 1 + int(percent.group(1)) / 100.0))
    sku_match = re.search(r"(?:sku|артикул)\s*[:№-]?\s*([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9_-]{4,23})", query, re.IGNORECASE)
    if not sku_match:
        sku_match = re.search(r"\b([0-9]{6,12})\b", query)
    sku = sku_match.group(1).rstrip("_") if sku_match else str(current.get("selected_sku", ""))
    limit_match = re.search(r"(?:топ|первые|до)\s*(\d{1,2})", text)
    limit = max(1, min(20, int(limit_match.group(1)))) if limit_match else 10
    threshold_match = re.search(r"(?:порог|старше|за)\s*(\d{1,3})\s*дн", text)
    threshold = max(1, min(365, int(threshold_match.group(1)))) if threshold_match else 14

    if re.search(r"сценар|сравн|разниц|изменил|что если|что измен", text):
        baseline = baseline_scenario or base_scenario
        args = {
            "supplier": supplier,
            "baseline_horizon_days": baseline.get("horizon_days", 30),
            "scenario_horizon_days": current.get("horizon_days", base_scenario.get("horizon_days", 30)),
            "baseline_demand_multiplier": baseline.get("demand_multiplier", 1.0),
            "scenario_demand_multiplier": current.get("demand_multiplier", base_scenario.get("demand_multiplier", 1.0)),
        }
        return "compare_replenishment_scenarios", args
    if re.search(r"аномал|выброс|крупн.*продаж|исключил|поправк", text):
        return "review_sales_anomalies", {"supplier": supplier, "horizon_days": current.get("horizon_days", 30), "limit": limit}
    if re.search(r"качест|свеж|устар|давно|обнов|пробел|не хват|непол", text):
        return "audit_replenishment_data", {"supplier": supplier, "freshness_threshold_days": threshold}
    if re.search(r"\bsku\b|артикул|почему.*заказ|объясни.*пози|объясн.*товар", text):
        return "explain_sku", {"sku": sku, "supplier": supplier, "horizon_days": current.get("horizon_days", 30), "demand_multiplier": current.get("demand_multiplier", 1.0)}
    if re.search(r"csv|экспорт|согласован|черновик заказа|подготов.*заказ", text):
        return "prepare_manual_approval_export", {"supplier": supplier, "horizon_days": current.get("horizon_days", 30), "demand_multiplier": current.get("demand_multiplier", 1.0)}
    return "summarize_replenishment_plan", {"supplier": supplier, "horizon_days": current.get("horizon_days", 30), "demand_multiplier": current.get("demand_multiplier", 1.0)}


def _answer_for(mode: str, facts: list[str]) -> str:
    if mode == "compare_replenishment_scenarios":
        return "Сравнил базовый план с новым сценарием по тем же данным."
    if mode == "review_sales_anomalies":
        return "Показал месячные поправки, которые алгоритм исключил из регулярного спроса."
    if mode == "audit_replenishment_data":
        return "Проверил полноту полей и свежесть снимков остатков."
    if mode == "explain_sku":
        return "Разобрал расчёт выбранной позиции по исходным числам."
    if mode == "prepare_manual_approval_export":
        return "Подготовил текущий план для CSV на ручное согласование."
    return "Сводка плана пополнения и позиции, требующие внимания."


def run_demo_agent(query: str, catalog: dict, base_scenario: dict, baseline_scenario: dict | None = None) -> dict:
    """Offline path, with the same deterministic tools as the OpenAI path."""
    name, arguments = _local_tool_selection(query, catalog, base_scenario, baseline_scenario)
    executed = _execute_tool(name, arguments, catalog, base_scenario)
    return {
        "mode": "demo",
        "intent": name,
        "answer": _answer_for(name, executed["facts"]),
        "facts": executed["facts"],
        "details": executed["details"],
        "tool": executed["tool"],
        "result": executed["result"],
    }


def run_openai_agent(query: str, catalog: dict, base_scenario: dict, api_key: str, baseline_scenario: dict | None = None) -> dict:
    instructions = (
        "Ты — агент поддержки менеджера закупа. Для каждого запроса выбери ровно один подходящий инструмент; "
        "все количества и проверки выполняет код инструмента. Не выдумывай данные. Используй только факты из его результата. "
        "Кратко ответь по-русски, перечисли срочные позиции или изменения, если они есть. "
        "Для проверки свежести называй 14 дней демонстрационным порогом, если инструмент использовал его. "
        "Данные о продажах агрегированы; не утверждай, что известен конкретный клиент. Не отправляй заказ поставщику. "
        "Не показывай внутреннюю цепочку рассуждений."
    )
    first = _api_request({
        "model": os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
        "instructions": instructions,
        "input": (
            f"Запрос менеджера: {query}\nТекущий интерфейсный сценарий: {json.dumps(base_scenario, ensure_ascii=False)}\n"
            f"Предыдущий сценарий для сравнения: {json.dumps(baseline_scenario or {}, ensure_ascii=False)}"
        ),
        "tools": TOOLS,
        "tool_choice": "required",
        "parallel_tool_calls": False,
        "store": False,
    }, api_key)
    calls = [item for item in first.get("output", []) if item.get("type") == "function_call"]
    if len(calls) != 1 or calls[0].get("name") not in TOOL_NAMES:
        raise ValueError("Expected one assistant tool call")
    try:
        arguments = json.loads(calls[0].get("arguments", "{}"))
    except json.JSONDecodeError as error:
        raise ValueError("Invalid function arguments") from error
    tool_name = calls[0]["name"]
    executed = _execute_tool(tool_name, arguments, catalog, base_scenario)
    call_output = {
        "type": "function_call_output",
        "call_id": calls[0].get("call_id"),
        "output": json.dumps({"facts": executed["facts"], "details": executed["details"], "warnings": executed["warnings"]}, ensure_ascii=False),
    }
    second = _api_request({
        "model": os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
        "instructions": instructions,
        "input": first.get("output", []) + [call_output],
        "store": False,
    }, api_key)
    answer = _response_text(second) or _answer_for(tool_name, executed["facts"])
    return {
        "mode": "openai",
        "intent": tool_name,
        "answer": answer,
        "facts": executed["facts"],
        "details": executed["details"],
        "tool": tool_name,
        "result": executed["result"],
    }


def run_agent(query: str, catalog: dict, base_scenario: dict, root: Path, baseline_scenario: dict | None = None) -> dict:
    load_local_env(root)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return run_demo_agent(query, catalog, base_scenario, baseline_scenario)
    started = time.perf_counter()
    try:
        result = run_openai_agent(query, catalog, base_scenario, api_key, baseline_scenario)
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        return result
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        fallback = run_demo_agent(query, catalog, base_scenario, baseline_scenario)
        fallback["mode"] = "demo_fallback"
        fallback["warning"] = "OpenAI недоступен, запрос обработан локальным помощником. Проверьте API-ключ и подключение позже."
        fallback["latency_ms"] = round((time.perf_counter() - started) * 1000)
        return fallback

