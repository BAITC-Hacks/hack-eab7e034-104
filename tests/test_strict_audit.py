"""Acceptance-oriented regressions. Not a claim that tests have been executed."""
from copy import deepcopy
from datetime import date
from replenishment import calculate_recommendations, forecast_demand, identity, _rounded_order


def item(**overrides):
    p = {'sku': '001_', 'supplier': 'IEK', 'warehouse': 'test', 'name': 'Fixture', 'unit': 'pcs',
         'purchase_unit': 'pcs', 'category': 'A', 'category_known': True,
         'current_stock': 0, 'stock_known': True, 'stock_snapshot': '2026-09-22',
         'inbound_known': True, 'inbound_shipments': [], 'in_transit': 0,
         'moq_known': True, 'order_multiple': 1, 'min_order': 0,
         'monthly_sales': {f'{y}-{m:02d}': 100 for y in (2024, 2025, 2026) for m in range(1, 13)}}
    p.update(overrides)
    return p


def cat(p):
    return {'schema_version': 2, 'data_mode': 'test', 'as_of_date': '2026-09-22', 'products': [p]}


def row(p):
    return calculate_recommendations(cat(p))['evaluated'][0]


def test_preserve_identifier_and_supplier_scope():
    assert identity(item()).split('|')[1] == '001_'
    assert identity(item()) != identity(item(supplier='Other'))


def test_late_inbound_does_not_reduce_near_term_order():
    baseline = row(item())
    late = row(item(inbound_shipments=[{'eta': '2027-01-01', 'quantity': 10000}]))
    assert baseline['recommended_qty'] == late['recommended_qty']
    assert late['trace']['late_inbound'] == 10000


def test_unknown_inbound_is_not_assumed_to_arrive_now():
    p = item(inbound_shipments=None, in_transit=1000)
    result = row(p)
    assert result['in_transit'] == 0
    assert result['approval_blockers']


def test_current_stock_unknown_never_becomes_verified_zero():
    result = calculate_recommendations(cat(item(current_stock=None, stock_known=True)))
    assert not result['recommendations']
    assert result['incomplete'][0]['missing'] == 'Остаток'


def test_business_growth_changes_single_month_forecast():
    when = date(2026, 9, 1)
    a = forecast_demand(item(growth_factor=1.0), as_of=when, horizon_days=10)
    b = forecast_demand(item(growth_factor=1.2), as_of=when, horizon_days=10)
    assert abs(b['forecast_units'] / a['forecast_units'] - 1.2) < 1e-5
    assert b['growth_source'] == 'provided_replace'


def test_future_stockout_cannot_leak_into_training():
    p = item()
    args = dict(as_of=date(2026,9,22), horizon_days=30)
    a = forecast_demand(p, **args)
    b = forecast_demand(p, **args, stockout_months={'2027-01': 31})
    assert a['forecast_units'] == b['forecast_units']
    assert b['stockout_compensation_units'] == 0


def test_minimum_and_multiple_are_different_constraints():
    assert _rounded_order(13, multiple=6, minimum=20) == 24
    assert _rounded_order(0, multiple=6, minimum=20) == 0


def test_category_policy_changes_calculation():
    c = cat(item())
    a = calculate_recommendations(c)['evaluated'][0]
    c['category_policies'] = {'IEK|A': {'review_days': 60, 'buffer_pct': .3, 'confirmed': True}}
    b = calculate_recommendations(c)['evaluated'][0]
    assert b['recommended_qty'] > a['recommended_qty']


def test_historical_snapshot_blocks_real_approval():
    c = cat(item(stock_snapshot='2026-09-01'))
    c['data_mode'] = 'supplier_reports'
    assert not calculate_recommendations(c)['evaluated'][0]['ready_for_approval']


def test_no_nonsensical_total_across_units():
    c = cat(item())
    c['products'].append(item(sku='002_', unit='m', purchase_unit='m'))
    metrics = calculate_recommendations(c)['metrics']
    assert metrics['recommended_units'] is None
    assert set(metrics['units_by_unit']) == {'pcs', 'm'}
