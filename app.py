"""Local web application for supplier replenishment planning."""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from agent import load_local_env, run_agent
from replenishment import calculate_recommendations


ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
CATALOG_PATH = ROOT / "data" / "demo_catalog.json"
MAX_REQUEST_BYTES = 1_000_000
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("replenishment")


def load_catalog(path: Path = CATALOG_PATH) -> dict:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        compressed_path = path.with_suffix(path.suffix + ".gz")
        with gzip.open(compressed_path, "rt", encoding="utf-8") as source:
            return json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Demo catalog is unavailable. Rebuild data/demo_catalog.json from the supplier archives.") from exc


CATALOG = load_catalog()
PRODUCT_BY_SKU = {str(item["sku"]): item for item in CATALOG.get("products", [])}


def openai_ready() -> bool:
    load_local_env(ROOT)
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def clean_stockouts(raw: object) -> tuple[dict, list[str]]:
    if raw in (None, {}):
        return {}, []
    warnings = []
    if not isinstance(raw, dict):
        return {}, ["Stockout-CSV должен содержать строки в формате sku,month,days_out."]
    cleaned = {}
    for sku, periods in raw.items():
        product = PRODUCT_BY_SKU.get(str(sku).strip())
        if product is None:
            warnings.append(f"Stockout-строка пропущена: SKU {str(sku)[:24]} не найден.")
            continue
        if not isinstance(periods, dict):
            warnings.append(f"Stockout-строки для SKU {product['sku']} пропущены.")
            continue
        valid = {}
        for period, raw_days in periods.items():
            if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", str(period)):
                warnings.append(f"Stockout-строка для SKU {product['sku']} пропущена: неверный месяц.")
                continue
            try:
                days = int(raw_days)
            except (TypeError, ValueError):
                warnings.append(f"Stockout-строка для SKU {product['sku']} пропущена: дни должны быть числом.")
                continue
            month = int(period[-2:])
            import calendar
            if not 0 <= days <= calendar.monthrange(int(period[:4]), month)[1]:
                warnings.append(f"Stockout-строка для SKU {product['sku']} пропущена: неверное число дней.")
                continue
            valid[str(period)] = days
        if valid:
            cleaned[product["sku"]] = valid
    return cleaned, warnings


class ReplenishmentHandler(BaseHTTPRequestHandler):
    server_version = "StockPilot/1.0"

    def log_message(self, _format: str, *_args) -> None:
        # Avoid writing user text, SKU details, or secrets to access logs.
        return

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Размер запроса пустой или превышает 1 МБ.")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Ожидался JSON-объект.")
        return payload

    def _send_file(self, path: Path) -> None:
        content_types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_types.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self._send_json(200, {
                "status": "ok",
                "as_of_date": CATALOG.get("as_of_date"),
                "products": len(CATALOG.get("products", [])),
                "openai_enabled": openai_ready(),
                "sources": CATALOG.get("sources", []),
                "data_limits": CATALOG.get("data_limits", []),
            })
            return
        if path == "/api/products":
            self._send_json(200, {"products": [
                {key: item.get(key) for key in ("sku", "supplier", "supplier_sku", "name", "unit", "category", "current_stock", "in_transit")}
                for item in CATALOG.get("products", [])
            ]})
            return
        if path in ("/", "/index.html"):
            target = FRONTEND / "index.html"
        elif path.startswith("/assets/"):
            requested = (FRONTEND / unquote(path.removeprefix("/assets/"))).resolve()
            if FRONTEND.resolve() not in requested.parents:
                self._send_json(404, {"error": "Файл не найден."})
                return
            target = requested
        else:
            self._send_json(404, {"error": "Страница не найдена."})
            return
        if not target.is_file():
            self._send_json(404, {"error": "Интерфейс не найден. Проверьте папку frontend."})
            return
        self._send_file(target)

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except Exception as exc:
            LOGGER.error("event=api_failure exception_type=%s", type(exc).__name__)
            self._send_json(500, {"error": "Не удалось обработать расчёт. Проверьте входные данные и повторите запрос."})

    def _handle_post(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Не удалось прочитать запрос. Проверьте формат данных и повторите."})
            return
        stockouts, stockout_warnings = clean_stockouts(payload.get("stockouts_by_sku", {}))
        scenario = payload.get("scenario") if isinstance(payload.get("scenario"), dict) else {}
        scenario = {**scenario, "stockouts_by_sku": stockouts}
        started = time.perf_counter()
        if path == "/api/recommendations":
            result = calculate_recommendations(CATALOG, scenario)
            result["warnings"].extend(stockout_warnings)
            elapsed = round((time.perf_counter() - started) * 1000)
            LOGGER.info("event=recommendations status=ok rows=%s latency_ms=%s", len(result["recommendations"]), elapsed)
            self._send_json(200, result)
            return
        if path == "/api/agent":
            query = str(payload.get("query", "")).strip()
            if not query:
                self._send_json(400, {"error": "Введите вопрос или запрос к закупочному агенту."})
                return
            if len(query) > 1200:
                self._send_json(400, {"error": "Запрос слишком длинный. Ограничение — 1 200 символов."})
                return
            baseline_scenario = payload.get("baseline_scenario") if isinstance(payload.get("baseline_scenario"), dict) else None
            if baseline_scenario is not None:
                baseline_scenario = {**baseline_scenario, "stockouts_by_sku": stockouts}
            result = run_agent(query, CATALOG, scenario, ROOT, baseline_scenario)
            result["result"]["warnings"].extend(stockout_warnings)
            latency = round((time.perf_counter() - started) * 1000)
            LOGGER.info("event=agent mode=%s tool=%s rows=%s latency_ms=%s", result.get("mode"), result.get("tool"), len(result["result"]["recommendations"]), latency)
            self._send_json(200, result)
            return
        self._send_json(404, {"error": "Маршрут API не найден."})


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    try:
        server = ThreadingHTTPServer((host, port), ReplenishmentHandler)
    except OSError:
        raise SystemExit("Не удалось запустить локальный сервер на выбранном порту.")
    LOGGER.info("event=server_started host=%s port=%s products=%s", host, port, len(CATALOG.get("products", [])))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("event=server_stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Supplier replenishment planner")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    run_server(args.host, args.port)
