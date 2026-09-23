
from __future__ import annotations
import json, math, re
from pathlib import Path
from datetime import date
import numpy as np
import pandas as pd

MONTH_NAMES = {
    1:"янв",2:"фев",3:"мар",4:"апр",5:"май",6:"июн",
    7:"июл",8:"авг",9:"сен",10:"окт",11:"ноя",12:"дек"
}

class ReplenishmentEngine:
    """Deterministic calculation core. The LLM never performs these calculations."""

    def __init__(self, data_path: str | Path):
        self.data_path = Path(data_path)
        self.data = json.loads(self.data_path.read_text(encoding="utf-8"))
        self.products = pd.DataFrame(self.data["products"])
        self.monthly = pd.DataFrame(self.data["monthly_sales"])
        if not self.monthly.empty:
            self.monthly["date"] = pd.to_datetime(self.monthly["date"])
            self.monthly["sales"] = pd.to_numeric(self.monthly["sales"], errors="coerce").fillna(0).clip(lower=0)
        for c in ["stock","in_transit","growth","seasonality","avg_12m","price","moq"]:
            if c in self.products:
                self.products[c] = pd.to_numeric(self.products[c], errors="coerce").fillna(0)
        self.products["moq"] = self.products["moq"].replace(0,1)

    @staticmethod
    def _robust_outliers(values: np.ndarray) -> tuple[np.ndarray, bool]:
        values = np.asarray(values, dtype=float)
        if len(values) < 6:
            return values, False
        med = np.median(values)
        mad = np.median(np.abs(values-med))
        if mad < 1e-9:
            q1, q3 = np.percentile(values,[25,75])
            threshold = q3 + 2.5*(q3-q1)
            mask = values > threshold
        else:
            robust_z = np.abs(0.6745*(values-med)/mad)
            mask = robust_z > 4.5
        if not mask.any():
            return values, False
        cleaned = values.copy()
        # Replace only extreme spikes with a robust local level. This protects seasonal peaks
        # because monthly seasonality is estimated before this operation.
        replacement = float(np.median(values[~mask])) if (~mask).any() else med
        cleaned[mask] = replacement
        return cleaned, True

    def _series(self, sku: str) -> pd.DataFrame:
        s = self.monthly[self.monthly.sku.astype(str)==str(sku)].copy()
        if s.empty:
            return pd.DataFrame(columns=["date","sales","month"])
        s["month"] = s.date.dt.month
        s = s.sort_values("date")
        # complete missing months as zero only inside observed date range
        idx = pd.date_range(s.date.min(), s.date.max(), freq="MS")
        s = s.set_index("date").reindex(idx, fill_value=0).rename_axis("date").reset_index()
        s["month"] = s.date.dt.month
        s["sales"] = pd.to_numeric(s["sales"], errors="coerce").fillna(0).clip(lower=0)
        return s

    def _seasonality(self, s: pd.DataFrame) -> tuple[float, dict[int,float]]:
        if s.empty or s.sales.sum() <= 0:
            return 1.0, {m:1.0 for m in range(1,13)}
        # Estimate seasonal factors by month-of-year, normalized to mean 1.
        overall = float(s.sales.mean())
        raw = {}
        for m in range(1,13):
            vals = s.loc[s.month==m, "sales"].values
            raw[m] = float(np.mean(vals)) if len(vals) and np.mean(vals)>0 else overall
        mean_factor = float(np.mean(list(raw.values()))) if raw else overall
        factors = {m: (raw[m]/mean_factor if mean_factor else 1.0) for m in raw}
        return float(factors.get(date.today().month,1.0)), factors

    def _forecast(self, p: pd.Series, lead_days:int, review_days:int, growth_weight:float):
        s=self._series(p.sku)
        if s.empty:
            raw_avg=float(p.get("avg_12m",0))
            return max(raw_avg,0), 1.0, 1.0, 0.0, False, {}
        # last 24 months; seasonality is estimated first, then deseasonalized outliers are removed
        s=s.tail(24).copy()
        _, season_factors=self._seasonality(s)
        s["sf"]=s.month.map(season_factors).fillna(1.0)
        s["deseason"]=s.sales/s.sf.replace(0,1)
        clean, outlier=self._robust_outliers(s.deseason.values)
        s["clean"]=clean
        # Weighted recency average on deseasonalized demand.
        weights=np.linspace(0.5,1.0,len(s))
        base=float(np.average(s.clean,weights=weights)) if len(s) else 0.0
        # Robust trend from the last 12 deseasonalized months.
        t=s.tail(12).reset_index(drop=True)
        trend_factor=1.0
        if len(t)>=6 and t.clean.mean()>0:
            x=np.arange(len(t),dtype=float)
            slope=np.polyfit(x,t.clean.values,1)[0]
            trend_factor=float(np.clip(1.0 + slope/max(t.clean.mean(),1e-9)*6, 0.75, 1.35))
        provided_growth=float(p.get("growth",0))
        growth_factor=float(np.clip(1.0 + provided_growth*growth_weight, 0.70, 1.40))
        # Combine statistical trend with provided business growth coefficient.
        combined_growth=float(np.clip(trend_factor * growth_factor, 0.70, 1.50))
        # Forecast the next calendar month after the latest observation.
        next_month = int((s.date.max().month % 12)+1)
        seasonal=float(season_factors.get(next_month,1.0))
        forecast_month=max(0.0,base*combined_growth*seasonal)
        # Estimate lost demand from stockout months. The engine uses expected demand for those
        # months rather than observed sales (which are censored by zero stock).
        stockout_months=p.get("stockout_months",[]) or []
        lost=0.0
        if stockout_months:
            for ym in stockout_months:
                try:
                    m=int(str(ym)[5:7])
                    lost += max(0.0, base*combined_growth*season_factors.get(m,1.0))
                except Exception:
                    pass
        # Spread historical lost demand over a year as an uplift to the monthly run-rate.
        lost_uplift=lost/12.0
        forecast_month += lost_uplift
        return forecast_month, seasonal, combined_growth, lost_uplift, outlier, {
            "base_deseasonalized":base,
            "trend_factor":trend_factor,
            "provided_growth":provided_growth,
            "next_month":next_month,
            "season_factors":season_factors,
            "lost_demand_estimate":lost,
        }

    def recommend(self, settings, category=None, supplier=None, search=None):
        ps=self.products.copy()
        if category and category!="ALL":
            ps=ps[ps.category.astype(str)==str(category)]
        if supplier and supplier!="ALL":
            ps=ps[ps.supplier.astype(str)==str(supplier)]
        if search:
            q=str(search).lower()
            ps=ps[ps.apply(lambda r:q in f"{r.sku} {r.supplier_sku} {r['name']}".lower(),axis=1)]
        rows=[]
        for _,p in ps.iterrows():
            forecast,seasonal,growth,lost,outlier,details=self._forecast(
                p,settings.lead_days,settings.review_days,settings.growth_weight)
            daily=forecast/30.4375
            protection_days=settings.lead_days+settings.review_days
            demand_protection=daily*protection_days
            # Demand variability from clean monthly series.
            s=self._series(p.sku).tail(24)
            std=float(s.sales.std(ddof=1)) if len(s)>1 else forecast*0.3
            safety=max(forecast*0.10, 0.35*std*math.sqrt(settings.lead_days/30))
            safety*=settings.service_factor
            available=max(float(p.stock)-float(p.get("reserved",0)),0)
            incoming=max(float(p.in_transit),0)
            gross=max(0.0,demand_protection+safety-available-incoming)
            moq=max(float(p.get("moq",1)),1.0)
            qty=math.ceil(gross/moq)*moq if gross>0 else 0.0
            lead_demand=daily*settings.lead_days
            coverage=(available+incoming)/max(daily,1e-9)
            if qty<=0:
                urgency="LOW"
            elif available < lead_demand*0.5:
                urgency="CRITICAL"
            elif available < lead_demand:
                urgency="HIGH"
            elif available < demand_protection:
                urgency="MEDIUM"
            else:
                urgency="LOW"
            risk=float(np.clip(
                100*(0.55*max(0,1-coverage/max(settings.lead_days,1))
                     +0.25*min(abs(growth-1),0.6)/0.6
                     +0.20*(1 if lost>0 else 0)),0,100))
            expl=(
                f"Прогноз {forecast:,.1f} ед./мес.: базовый спрос {details.get('base_deseasonalized',0):,.1f}, "
                f"сезонность ×{seasonal:.2f}, рост ×{growth:.2f}. "
                f"Защищаемый период {protection_days} дн. → {demand_protection:,.1f} ед.; "
                f"страховой запас {safety:,.1f}. Доступно {available:,.1f}, в пути {incoming:,.1f}."
            )
            if lost>0:
                expl+=f" Компенсация исторического stockout +{lost:,.1f} ед./мес."
            if outlier:
                expl+=" Выбросы очищены робастным методом и не влияют на регулярную потребность."
            if qty>0:
                expl+=f" Рекомендовано {qty:,.0f} ед. с учётом MOQ {moq:,.0f}."
            else:
                expl+=" Заказ не требуется."
            rows.append({
                "sku":str(p.sku),"supplier_sku":str(p.supplier_sku),"name":str(p["name"]),
                "category":str(p.category),"supplier":str(p.supplier),
                "recommended_qty":float(qty),"moq":float(moq),"urgency":urgency,
                "risk_score":round(risk,1),"forecast_monthly":round(forecast,2),
                "demand_protection":round(demand_protection,2),"stock":float(p.stock),
                "in_transit":float(p.in_transit),"lost_demand":round(lost,2),
                "seasonality_factor":round(seasonal,3),"growth_factor":round(growth,3),
                "outlier_flag":bool(outlier),"explanation":expl,
                "calculation":{
                    "base":details.get("base_deseasonalized",0),
                    "trend_factor":details.get("trend_factor",1),
                    "provided_growth":details.get("provided_growth",0),
                    "seasonality":seasonal,"lost_demand_uplift":lost,
                    "protection_days":protection_days,"safety_stock":safety,
                    "available_stock":available,"in_transit":incoming,
                    "gross_order":gross,"moq":moq
                }
            })
        rows.sort(key=lambda x:(-({"CRITICAL":4,"HIGH":3,"MEDIUM":2,"LOW":1}[x["urgency"]]),-x["risk_score"],-x["recommended_qty"]))
        return rows[:settings.max_rows]

    def item(self, sku, settings):
        rows=self.recommend(settings,search=sku)
        for r in rows:
            if r["sku"]==str(sku):
                return r
        raise KeyError(sku)

    def overview(self, settings):
        rows=self.recommend(settings)
        orders=[r for r in rows if r["recommended_qty"]>0]
        return {
            "products_analyzed":len(self.products),
            "recommendations":len(orders),
            "total_units":round(sum(r["recommended_qty"] for r in orders),1),
            "critical":sum(r["urgency"]=="CRITICAL" for r in orders),
            "high":sum(r["urgency"]=="HIGH" for r in orders),
            "medium":sum(r["urgency"]=="MEDIUM" for r in orders),
            "low":sum(r["urgency"]=="LOW" for r in orders),
            "suppliers":sorted(self.products.supplier.dropna().astype(str).unique().tolist()),
            "categories":sorted(self.products.category.dropna().astype(str).unique().tolist()),
        }
