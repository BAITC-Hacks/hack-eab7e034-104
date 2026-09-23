from __future__ import annotations

import calendar
import unittest
from datetime import date

from replenishment import calculate_recommendations, forecast_demand


def product(monthly_sales: dict[str, float] | None = None, **overrides) -> dict:
    values = {
        "sku": "200400085",
        "supplier": "IEK",
        "name": "Тестовый товар",
        "supplier_sku": "TEST-01",
        "category": "Тест",
        "category_known": True,
        "unit": "шт.",
        "moq": 1.0,
        "moq_known": True,
        "current_stock": 0.0,
        "stock_known": True,
        "stock_snapshot": "2026-09",
        "stock_basis": "Тестовый остаток",
        "in_transit": 0.0,
        "inbound_known": True,
        "monthly_sales": monthly_sales or {
            f"{year:04d}-{month:02d}": 100.0
            for year, month in ((year, month) for year in (2024, 2025, 2026) for month in range(1, 13))
            if (year, month) <= (2026, 9)
        },
        "one_off_adjustments": {},
        "detected_large_orders": 0,
    }
    values.update(overrides)
    return values


def catalog(item: dict, as_of: str = "2026-09-22") -> dict:
    return {"as_of_date": as_of, "products": [item], "data_limits": []}


def recommendation(result: dict) -> dict | None:
    rows = result["recommendations"]
    return rows[0] if rows else None


class ForecastTests(unittest.TestCase):
    def test_calendar_seasonality_changes_forecast(self):
        sales = {f"{year:04d}-{month:02d}": 100.0 for year in (2024, 2025, 2026) for month in range(1, 13)}
        sales["2025-01"] = 250.0
        sales["2026-01"] = 250.0
        forecast = forecast_demand(product(sales), as_of=date(2026, 11, 30), horizon_days=45)
        self.assertGreater(forecast["seasonality_factor"], 1.0)

    def test_sustained_growth_is_damped_into_horizon(self):
        sales = {f"{year:04d}-{month:02d}": 100.0 for year in (2025, 2026) for month in range(1, 13)}
        for month in range(3, 9):
            sales[f"2026-{month:02d}"] = 180.0
        forecast = forecast_demand(product(sales), as_of=date(2026, 9, 22), horizon_days=30)
        self.assertGreater(forecast["growth_factor"], 1.0)
        self.assertLessEqual(forecast["growth_factor"], 1.35)

    def test_stockout_compensation_increases_demand(self):
        item = product()
        baseline = forecast_demand(item, as_of=date(2026, 9, 22), horizon_days=30)
        corrected = forecast_demand(item, as_of=date(2026, 9, 22), horizon_days=30, stockout_months={"2026-08": 10})
        self.assertGreater(corrected["forecast_units"], baseline["forecast_units"])
        self.assertGreater(corrected["stockout_compensation_units"], 0)

    def test_one_off_order_is_removed_from_regular_forecast(self):
        sales = {f"{year:04d}-{month:02d}": 100.0 for year in (2025, 2026) for month in range(1, 10)}
        sales["2026-08"] = 2100.0
        plain = product(sales)
        flagged = product(sales, one_off_adjustments={"2026-08": 2000.0})
        plain_forecast = forecast_demand(plain, as_of=date(2026, 9, 22), horizon_days=30)
        flagged_forecast = forecast_demand(flagged, as_of=date(2026, 9, 22), horizon_days=30)
        self.assertLessEqual(flagged_forecast["forecast_units"], plain_forecast["forecast_units"])
        self.assertGreater(flagged_forecast["anomaly_removed_units"], 0)

    def test_monthly_spike_is_robustly_capped(self):
        regular_sales = {f"{year:04d}-{month:02d}": 100.0 for year in (2025, 2026) for month in range(1, 10)}
        spike_sales = {**regular_sales, "2026-08": 5000.0}
        regular = forecast_demand(product(regular_sales), as_of=date(2026, 9, 22), horizon_days=30)
        spike = forecast_demand(product(spike_sales), as_of=date(2026, 9, 22), horizon_days=30)
        self.assertLess(spike["forecast_units"], regular["forecast_units"] * 1.15)
        self.assertGreater(spike["anomaly_removed_units"], 0)


class ReplenishmentTests(unittest.TestCase):
    def test_recommendations_are_grouped_by_supplier(self):
        systeme = product(sku="SYS-1", supplier="Systeme Electric")
        result = calculate_recommendations({"as_of_date": "2026-09-22", "products": [systeme, product()]})
        suppliers = [row["supplier"] for row in result["recommendations"]]
        self.assertEqual(suppliers, sorted(suppliers))

    def test_in_transit_reduces_order_quantity(self):
        baseline = recommendation(calculate_recommendations(catalog(product())))
        with_inbound = recommendation(calculate_recommendations(catalog(product(in_transit=25.0))))
        self.assertIsNotNone(baseline)
        self.assertEqual(with_inbound["recommended_qty"], baseline["recommended_qty"] - 25.0)

    def test_stock_delta_and_demand_scenario_change_order(self):
        baseline = recommendation(calculate_recommendations(catalog(product())))
        more_stock = recommendation(calculate_recommendations(catalog(product()), {"selected_sku": "200400085", "stock_delta": 20}))
        higher_demand = recommendation(calculate_recommendations(catalog(product()), {"demand_multiplier": 1.2}))
        self.assertEqual(more_stock["recommended_qty"], baseline["recommended_qty"] - 20.0)
        self.assertGreater(higher_demand["recommended_qty"], baseline["recommended_qty"])

    def test_minimum_order_multiple_rounds_up(self):
        result = calculate_recommendations(catalog(product(moq=25.0)))
        row = recommendation(result)
        self.assertIsNotNone(row)
        self.assertEqual(row["recommended_qty"] % 25.0, 0.0)
        self.assertGreaterEqual(row["recommended_qty"], row["forecast_units"] * 1.15)

    def test_missing_stock_and_inbound_are_reported_as_incomplete(self):
        missing = product(stock_known=False, inbound_known=False)
        result = calculate_recommendations(catalog(missing))
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["incomplete"][0]["missing"], "Остаток")

    def test_zero_demand_does_not_create_an_order(self):
        no_sales = product({f"{year:04d}-{month:02d}": 0 for year in (2025, 2026) for month in range(1, 10)})
        result = calculate_recommendations(catalog(no_sales))
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["metrics"]["no_demand_products"], 1)

    def test_unknown_sku_scenario_is_ignored_with_warning(self):
        result = calculate_recommendations(catalog(product()), {"selected_sku": "UNKNOWN", "stock_delta": -100})
        self.assertTrue(any("SKU сценария не найден" in warning for warning in result["warnings"]))

    def test_stockout_month_bounds_follow_calendar(self):
        item = product()
        result = forecast_demand(item, as_of=date(2026, 9, 22), horizon_days=30, stockout_months={"2026-02": 28})
        self.assertGreaterEqual(result["stockout_compensation_units"], 0)
        self.assertEqual(calendar.monthrange(2026, 2)[1], 28)


if __name__ == "__main__":
    unittest.main()
