
from __future__ import annotations
import os, json
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from .models import CalculationSettings, ReplenishmentRequest, AgentRequest, ApproveRequest
from .engine import ReplenishmentEngine
from .agent import LogisticsAgent

BASE=Path(__file__).resolve().parents[1]
DATA=BASE/"data"/"demo.json"
engine=ReplenishmentEngine(DATA)
agent=LogisticsAgent(engine)

app=FastAPI(title="StockPilot AI — HackAlem Logistics", version="1.0.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])

@app.get("/health")
def health():
    return {"status": "ok", "service": "stockpilot-ai", "version": app.version}

@app.get("/")
def index(): return FileResponse(BASE/"static"/"index.html")

@app.get("/api/overview")
def overview():
    s=CalculationSettings()
    return engine.overview(s)

@app.post("/api/recommendations")
def recommendations(req: ReplenishmentRequest):
    return {"items":engine.recommend(req,req.category,req.supplier,req.search)}

@app.get("/api/item/{sku}")
def item(sku:str,lead_days:int=30,review_days:int=30):
    try: return engine.item(sku,CalculationSettings(lead_days=lead_days,review_days=review_days))
    except KeyError: raise HTTPException(404,"SKU not found")

@app.post("/api/agent")
async def agent_chat(req:AgentRequest):
    return await agent.chat(req.message,req.settings)

@app.post("/api/approve")
def approve(req:ApproveRequest):
    # Safety boundary: approval creates a local confirmation payload only.
    # No supplier communication is performed.
    return {"status":"approved_for_export","items":[x.model_dump() for x in req.items],
            "message":"Заказ подтверждён для экспорта. Автоматической отправки поставщику нет."}

@app.get("/api/export")
def export_csv():
    import csv, io
    s=CalculationSettings(max_rows=1000)
    rows=[r for r in engine.recommend(s) if r["recommended_qty"]>0]
    buf=io.StringIO()
    writer=csv.DictWriter(buf,fieldnames=["supplier","sku","supplier_sku","name","category","recommended_qty","urgency","risk_score","explanation"])
    writer.writeheader()
    for r in rows: writer.writerow({k:r[k] for k in writer.fieldnames})
    from fastapi.responses import StreamingResponse
    return StreamingResponse(iter([buf.getvalue()]),media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=stockpilot_recommendations.csv"})
