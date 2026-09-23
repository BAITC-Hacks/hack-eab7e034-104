"""OpenAI function-calling agent and explicitly labelled offline demo mode."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from replenishment import calculate_recommendations


TOOL_NAME = "calculate_replenishment_plan"
TOOL = {
    "type": "function",
    "name": TOOL_NAME,
    "description": "Calculate supplier replenishment with the validated deterministic forecasting engine. Use this tool for every purchase quantity or scenario request. Never calculate quantities yourself.",
    "parameters": {
        "type": "object",
        "properties": {
            "supplier": {
                "type": "string",
                "enum": ["all", "Systeme Electric", "IEK"],
                "description": "Supplier scope. Use all unless the user names a supplier.",
            },
            "horizon_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 180,
                "description": "Supplier planning horizon in days, based on the user's request.",
            },
            "demand_multiplier": {
                "type": "number",
                "minimum": 0.5,
                "maximum": 3.0,
                "description": "Demand scenario multiplier; 1.0 means source demand, 1.15 means 15% higher.",
            },
        },
        "required": ["supplier", "horizon_days", "demand_multiplier"],
        "additionalProperties": False,
    },
    "strict": True,
}


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


def _safe_tool_arguments(arguments: Any, suppliers: set[str]) -> tuple[dict, list[str]]:
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    warnings = []
    supplier = arguments.get("supplier", "all")
    if supplier != "all" and supplier not in suppliers:
        supplier = "all"
        warnings.append("Не удалось подтвердить поставщика из запроса; рассчитаны все поставщики.")
    try:
        horizon = int(arguments.get("horizon_days", 30))
    except (TypeError, ValueError):
        horizon = 30
        warnings.append("Не удалось разобрать срок, использовано 30 дней.")
    bounded_horizon = max(1, min(180, horizon))
    if bounded_horizon != horizon:
        warnings.append("Срок ограничен диапазоном 1–180 дней.")
    try:
        multiplier = float(arguments.get("demand_multiplier", 1.0))
    except (TypeError, ValueError):
        multiplier = 1.0
        warnings.append("Не удалось разобрать изменение спроса, использовано 0%.")
    bounded_multiplier = max(0.5, min(3.0, multiplier))
    if bounded_multiplier != multiplier:
        warnings.append("Изменение спроса ограничено диапазоном от −50% до +200%.")
    return {"supplier": supplier, "horizon_days": bounded_horizon, "demand_multiplier": bounded_multiplier}, warnings


def _tool_summary(result: dict, scenario: dict) -> dict:
    top = [
        {
            "sku": row["sku"],
            "supplier": row["supplier"],
            "name": row["name"][:90],
            "quantity": row["recommended_qty"],
            "unit": row["unit"],
            "urgency": row["urgency"],
            "reason": row["explanation"],
        }
        for row in result["recommendations"][:10]
    ]
    return {
        "scenario": scenario,
        "metrics": result["metrics"],
        "top_recommendations": top,
        "incomplete_products": result["metrics"]["incomplete_products"],
        "warnings": result["warnings"],
        "data_limits": result["data_limits"],
    }


def run_openai_agent(query: str, catalog: dict, base_scenario: dict, api_key: str) -> dict:
    suppliers = {str(item.get("supplier", "")) for item in catalog.get("products", []) if item.get("supplier")}
    instructions = (
        "Ты — агент поддержки закупщика. Всегда вызывай calculate_replenishment_plan ровно один раз перед ответом. "
        "Не рассчитывай количества и не выдумывай данные. Инструмент применяет проверенный алгоритм. "
        "Объясни вывод по фактам из результата, перечисли срочные позиции, если они есть, и предупреди о нехватке исходных данных. "
        "Не утверждай и не отправляй заказ. Не раскрывай цепочку рассуждений. Отвечай кратко на русском языке."
    )
    first = _api_request({
        "model": os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
        "instructions": instructions,
        "input": (
            f"Запрос закупщика: {query}\n"
            f"Текущий интерфейсный сценарий: поставщик={base_scenario.get('supplier', 'all')}, "
            f"срок={base_scenario.get('horizon_days', 30)} дней, "
            f"множитель спроса={base_scenario.get('demand_multiplier', 1.0)}. "
            "Сохраняй текущие параметры, если пользователь не просит их изменить."
        ),
        "tools": [TOOL],
        "tool_choice": {"type": "function", "name": TOOL_NAME},
        "parallel_tool_calls": False,
        "store": False,
    }, api_key)
    calls = [item for item in first.get("output", []) if item.get("type") == "function_call"]
    if len(calls) != 1 or calls[0].get("name") != TOOL_NAME:
        raise ValueError("Expected one validated replenishment tool call")
    try:
        raw_arguments = json.loads(calls[0].get("arguments", "{}"))
    except json.JSONDecodeError as error:
        raise ValueError("Invalid function arguments") from error
    tool_scenario, warnings = _safe_tool_arguments(raw_arguments, suppliers)
    scenario = {**base_scenario, **tool_scenario}
    result = calculate_recommendations(catalog, scenario)
    summary = _tool_summary(result, tool_scenario)
    summary["warnings"] = warnings + summary["warnings"]
    call_output = {
        "type": "function_call_output",
        "call_id": calls[0].get("call_id"),
        "output": json.dumps(summary, ensure_ascii=False),
    }
    second = _api_request({
        "model": os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
        "instructions": instructions,
        # Replay the returned response items, including reasoning items, because
        # requests use store=false and do not retain server-side state.
        "input": first.get("output", []) + [call_output],
        "store": False,
    }, api_key)
    answer = _response_text(second)
    if not answer:
        answer = _local_summary(result)
        warnings.append("OpenAI не вернул текстовое объяснение; показано локальное.")
    result["warnings"].extend(warnings)
    return {"mode": "openai", "answer": answer, "tool": TOOL_NAME, "result": result}


def _local_summary(result: dict) -> str:
    metrics = result["metrics"]
    first = result["recommendations"][:3]
    if first:
        lines = [f"Рассчитано {metrics['recommendation_positions']} позиций. Срочных: {metrics['urgent_positions']}."]
        lines.extend(f"• {row['sku']} — {row['recommended_qty']:g} {row['unit']}, {row['urgency'].lower()}. {row['explanation']}" for row in first)
    else:
        lines = ["По выбранному сценарию дефицитных позиций для заказа не найдено."]
    if metrics["incomplete_products"]:
        lines.append(f"{metrics['incomplete_products']} SKU пропущены: не хватает остатка, продаж или данных о поставках.")
    return "\n".join(lines)


def _demo_scenario(query: str, base_scenario: dict, catalog: dict) -> dict:
    lowered = query.lower()
    supplier = base_scenario.get("supplier", "all")
    if re.search(r"\bie?k\b", lowered):
        supplier = "IEK"
    elif "systeme" in lowered or "систем" in lowered:
        supplier = "Systeme Electric"
    days_match = re.search(r"(\d{1,3})\s*(?:дн(?:ей|я)?|days?)", lowered)
    horizon_days = int(days_match.group(1)) if days_match else int(base_scenario.get("horizon_days", 30))
    percent_match = re.search(r"(?:на\s*)?([+-]?\d{1,3})\s*%", lowered)
    multiplier = 1 + (int(percent_match.group(1)) / 100.0) if percent_match else float(base_scenario.get("demand_multiplier", 1.0))
    sku_match = re.search(r"\b([0-9]{6,12}_?)\b", query)
    selected_sku = base_scenario.get("selected_sku", "")
    if sku_match:
        typed = sku_match.group(1).rstrip("_")
        if any(item.get("sku") == typed for item in catalog.get("products", [])):
            selected_sku = typed
    return {
        **base_scenario,
        "supplier": supplier,
        "horizon_days": horizon_days,
        "demand_multiplier": multiplier,
        "selected_sku": selected_sku,
    }


def run_demo_agent(query: str, catalog: dict, base_scenario: dict) -> dict:
    """Offline, deterministic path. The UI labels it as demo mode."""
    scenario = _demo_scenario(query, base_scenario, catalog)
    result = calculate_recommendations(catalog, scenario)
    return {
        "mode": "demo",
        "answer": _local_summary(result),
        "tool": "calculate_replenishment_plan (local demo)",
        "result": result,
    }


def run_agent(query: str, catalog: dict, base_scenario: dict, root: Path) -> dict:
    load_local_env(root)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return run_demo_agent(query, catalog, base_scenario)
    started = time.perf_counter()
    try:
        result = run_openai_agent(query, catalog, base_scenario, api_key)
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        return result
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        # Keep the user flow alive when an optional external API is unavailable.
        fallback = run_demo_agent(query, catalog, base_scenario)
        fallback["mode"] = "demo_fallback"
        fallback["warning"] = "OpenAI недоступен, расчёт выполнен локально. Проверьте API-ключ и подключение позже."
        fallback["latency_ms"] = round((time.perf_counter() - started) * 1000)
        return fallback
