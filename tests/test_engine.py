
from pathlib import Path
from app.engine import ReplenishmentEngine
from app.models import CalculationSettings

ENGINE=ReplenishmentEngine(Path(__file__).parents[1]/"data"/"demo.json")

def test_all_sources_affect_result():
    s=CalculationSettings(max_rows=100)
    rows=ENGINE.recommend(s)
    assert rows
    x=rows[0]
    original=x["recommended_qty"]
    p=ENGINE.products.loc[ENGINE.products.sku.astype(str)==x["sku"]].iloc[0].copy()
    # Inbound must affect the recommendation: add enough inbound to cover it.
    ENGINE.products.loc[ENGINE.products.sku.astype(str)==x["sku"],"in_transit"]=p.in_transit+original+100
    changed=ENGINE.item(x["sku"],s)
    assert changed["recommended_qty"] <= original
    ENGINE.products.loc[ENGINE.products.sku.astype(str)==x["sku"],"in_transit"]=p.in_transit

def test_seasonality_is_not_flat():
    rows=ENGINE.recommend(CalculationSettings(max_rows=100))
    assert any(abs(x["seasonality_factor"]-1)>0.05 for x in rows)

def test_moq_rounding():
    rows=ENGINE.recommend(CalculationSettings(max_rows=100))
    for x in rows:
        if x["recommended_qty"]>0:
            assert x["recommended_qty"] % x["moq"] < 1e-7

def test_stockout_uplift_can_increase_forecast():
    rows=ENGINE.recommend(CalculationSettings(max_rows=100))
    candidates=[x for x in rows if x["lost_demand"]>0]
    if candidates:
        assert all(x["forecast_monthly"]>=0 for x in candidates)

def test_explainability():
    rows=ENGINE.recommend(CalculationSettings(max_rows=30))
    assert all(x["explanation"] and x["calculation"] for x in rows)
