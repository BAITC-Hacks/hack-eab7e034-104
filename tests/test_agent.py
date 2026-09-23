from __future__ import annotations

import unittest
from unittest.mock import patch

from agent import run_demo_agent, run_openai_agent


def demo_catalog() -> dict:
    sales = {
        f"{year:04d}-{month:02d}": 100.0
        for year in (2025, 2026)
        for month in range(1, 10)
    }
    sales["2026-08"] = 2100.0
    sales["2026-07"] = 5000.0
    return {
        "as_of_date": "2026-09-22",
        "data_limits": ["В источнике нет обезличенного client_id."],
        "products": [{
            "sku": "200400085",
            "supplier": "IEK",
            "name": "Тестовый выключатель",
            "supplier_sku": "TEST-01",
            "category": "Автоматика",
            "category_known": True,
            "unit": "шт.",
            "moq": 1.0,
            "moq_known": True,
            "current_stock": 0,
            "stock_known": True,
            "stock_snapshot": "2026-09",
            "stock_basis": "Тестовый срез",
            "in_transit": 0,
            "inbound_known": True,
            "monthly_sales": sales,
            "one_off_adjustments": {"2026-08": 2000.0},
            "detected_large_orders": 1,
        }],
    }


class AssistantIntentTests(unittest.TestCase):
    def test_explains_a_sku_with_calculation_facts(self):
        result = run_demo_agent("Объясни SKU 200400085", demo_catalog(), {"horizon_days": 30, "demand_multiplier": 1.0})
        self.assertEqual(result["intent"], "explain_sku")
        self.assertTrue(any("Предложено заказать" in fact for fact in result["facts"]))

    def test_lists_anomaly_adjustment_and_client_data_limit(self):
        result = run_demo_agent("Какие выбросы алгоритм исключил?", demo_catalog(), {"horizon_days": 30, "demand_multiplier": 1.0})
        self.assertEqual(result["intent"], "review_sales_anomalies")
        self.assertGreater(result["details"]["total_events"], 0)
        self.assertGreater(result["details"]["event_counts"]["source"], 0)
        self.assertGreater(result["details"]["event_counts"]["spike_cap"], 0)
        self.assertTrue(any("Client ID" in fact for fact in result["facts"]))

    def test_explains_alphanumeric_supplier_sku(self):
        catalog = demo_catalog()
        catalog["products"][0]["sku"] = "ABCD-12345"
        result = run_demo_agent("Объясни SKU ABCD-12345", catalog, {"horizon_days": 30, "demand_multiplier": 1.0})
        self.assertEqual(result["intent"], "explain_sku")
        self.assertEqual(result["details"]["sku"], "ABCD-12345")

    def test_quality_audit_labels_threshold_as_demo_assumption(self):
        result = run_demo_agent("Проверь качество и свежесть данных", demo_catalog(), {})
        self.assertEqual(result["intent"], "audit_replenishment_data")
        self.assertEqual(result["details"]["threshold_days"], 14)
        self.assertTrue(any("демонстрационное допущение" in fact for fact in result["facts"]))

    def test_scenario_comparison_uses_two_deterministic_plans(self):
        result = run_demo_agent(
            "Что изменилось после сценария?",
            demo_catalog(),
            {"horizon_days": 90, "demand_multiplier": 1.15},
            {"horizon_days": 30, "demand_multiplier": 1.0},
        )
        self.assertEqual(result["intent"], "compare_replenishment_scenarios")
        self.assertNotEqual(
            result["details"]["baseline"]["metrics"]["recommended_units"],
            result["details"]["scenario"]["metrics"]["recommended_units"],
        )
        self.assertEqual(result["result"]["scenario"]["horizon_days"], 90)

    def test_export_is_draft_and_requires_human_approval(self):
        result = run_demo_agent("Подготовь CSV на ручное согласование", demo_catalog(), {})
        self.assertEqual(result["intent"], "prepare_manual_approval_export")
        self.assertTrue(result["details"]["approval_required"])
        self.assertTrue(any("вручную" in fact for fact in result["facts"]))

    def test_openai_path_executes_selected_tool_and_returns_validated_facts(self):
        first = {
            "output": [{
                "type": "function_call",
                "name": "audit_replenishment_data",
                "call_id": "call-1",
                "arguments": '{"supplier":"IEK","freshness_threshold_days":14}',
            }]
        }
        second = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Проверка выполнена."}]}]}
        with patch("agent._api_request", side_effect=[first, second]) as api_request:
            result = run_openai_agent("Проверь свежесть IEK", demo_catalog(), {}, "test-key")
        self.assertEqual(result["mode"], "openai")
        self.assertEqual(result["intent"], "audit_replenishment_data")
        self.assertEqual(result["answer"], "Проверка выполнена.")
        self.assertTrue(any("демонстрационное допущение" in fact for fact in result["facts"]))
        self.assertEqual(api_request.call_count, 2)


if __name__ == "__main__":
    unittest.main()

