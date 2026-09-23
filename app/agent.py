
from __future__ import annotations
import json, os
from typing import Any
from .engine import ReplenishmentEngine

class LogisticsAgent:
    """Small tool-using agent. Deterministic calculations remain in ReplenishmentEngine."""

    def __init__(self, engine: ReplenishmentEngine):
        self.engine=engine

    def tool_specs(self):
        return [
            {"type":"function","name":"get_overview","description":"Get current replenishment overview and available categories/suppliers.",
             "parameters":{"type":"object","properties":{},"additionalProperties":False}},
            {"type":"function","name":"calculate_replenishment","description":"Run deterministic supplier replenishment calculation. Use filters only when user asks for them.",
             "parameters":{"type":"object","properties":{
                 "category":{"type":"string"},"supplier":{"type":"string"},"search":{"type":"string"},
                 "lead_days":{"type":"integer","minimum":1,"maximum":180},
                 "review_days":{"type":"integer","minimum":1,"maximum":180},
                 "max_rows":{"type":"integer","minimum":1,"maximum":100}
             },"additionalProperties":False}},
            {"type":"function","name":"inspect_item","description":"Inspect one SKU and return the exact calculation trace and recommendation.",
             "parameters":{"type":"object","properties":{
                 "sku":{"type":"string"},
                 "lead_days":{"type":"integer","minimum":1,"maximum":180},
                 "review_days":{"type":"integer","minimum":1,"maximum":180}
             },"required":["sku"],"additionalProperties":False}},
        ]

    def execute_tool(self,name,args,default_settings):
        if name=="get_overview":
            return self.engine.overview(default_settings)
        if name=="calculate_replenishment":
            s=default_settings.model_copy()
            for k in ["lead_days","review_days","max_rows"]:
                if k in args and args[k] is not None: setattr(s,k,args[k])
            return self.engine.recommend(s, args.get("category"),args.get("supplier"),args.get("search"))
        if name=="inspect_item":
            s=default_settings.model_copy()
            for k in ["lead_days","review_days"]:
                if k in args and args[k] is not None: setattr(s,k,args[k])
            try: return self.engine.item(args["sku"],s)
            except KeyError: return {"error":"SKU not found","sku":args["sku"]}
        return {"error":"unknown tool"}

    async def chat(self,message:str,settings):
        api_key=os.getenv("OPENAI_API_KEY")
        if not api_key:
            # deterministic fallback keeps the demo usable without an API key
            rows=self.engine.recommend(settings)
            if any(w in message.lower() for w in ["почему","обосну","sku","артикул"]):
                return {"mode":"fallback","answer":"AI API не настроен. Я выполнил детерминированный расчёт. Укажите SKU для подробного расчёта.","tool_trace":["calculate_replenishment"],"data":rows[:8]}
            return {"mode":"fallback","answer":f"AI API не настроен. Расчёт выполнен локальным ядром: найдено {sum(r['recommended_qty']>0 for r in rows)} позиций к заказу.","tool_trace":["calculate_replenishment"],"data":rows[:12]}
        from openai import OpenAI
        client=OpenAI(api_key=api_key)
        system=(
            "Ты — AI-агент менеджера закупок. Используй только инструменты для расчётов. "
            "Никогда не придумывай числа. Не раскрывай chain-of-thought. "
            "Объясняй решения фактами из tool results. Не утверждай, что заказ отправлен: "
            "приложение только формирует рекомендацию, а подтверждение выполняет человек."
        )
        input_items=[{"role":"user","content":message}]
        trace=[]
        for _ in range(4):
            resp=client.responses.create(model=os.getenv("OPENAI_MODEL","gpt-5-mini"),
                instructions=system,input=input_items,tools=self.tool_specs())
            calls=[x for x in resp.output if getattr(x,"type",None)=="function_call"]
            if not calls:
                return {"mode":"openai","answer":resp.output_text,"tool_trace":trace,"data":[]}
            for call in calls:
                args=json.loads(call.arguments or "{}")
                result=self.execute_tool(call.name,args,settings)
                trace.append(call.name)
                input_items.append(call)
                input_items.append({"type":"function_call_output","call_id":call.call_id,"output":json.dumps(result,ensure_ascii=False,default=str)})
        return {"mode":"openai","answer":"Не удалось завершить агентный цикл за допустимое число шагов.","tool_trace":trace,"data":[]}
