
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class CalculationSettings(BaseModel):
    lead_days: int = Field(30, ge=1, le=180)
    review_days: int = Field(30, ge=1, le=180)
    service_factor: float = Field(1.15, ge=1.0, le=2.0)
    growth_weight: float = Field(1.0, ge=0.0, le=1.5)
    max_rows: int = Field(100, ge=1, le=1000)

class ReplenishmentRequest(CalculationSettings):
    category: Optional[str] = None
    supplier: Optional[str] = None
    search: Optional[str] = None

class Adjustment(BaseModel):
    sku: str
    quantity: float = Field(ge=0)

class ApproveRequest(BaseModel):
    items: List[Adjustment]

class AgentRequest(BaseModel):
    message: str
    settings: CalculationSettings = CalculationSettings()

class Recommendation(BaseModel):
    sku: str
    supplier_sku: str
    name: str
    category: str
    supplier: str
    recommended_qty: float
    moq: float
    urgency: str
    risk_score: float
    forecast_monthly: float
    demand_protection: float
    stock: float
    in_transit: float
    lost_demand: float
    seasonality_factor: float
    growth_factor: float
    outlier_flag: bool
    explanation: str
    calculation: Dict[str, Any]
