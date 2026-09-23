from __future__ import annotations

import http.client
import gzip
import json
import threading
import tempfile
from pathlib import Path
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app


class ApiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.ReplenishmentHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method: str, path: str, payload: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type", "")
        connection.close()
        return response.status, content_type, raw

    def test_health_and_dashboard_load(self):
        status, content_type, raw = self.request("GET", "/api/health")
        health = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertGreaterEqual(health["products"], 3900)
        self.assertEqual(health["status"], "ok")
        status, content_type, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("План пополнения склада".encode("utf-8"), raw)

    def test_catalog_loader_reads_compressed_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            with gzip.open(path.with_suffix(".json.gz"), "wt", encoding="utf-8") as target:
                json.dump({"products": [{"sku": "sample"}]}, target)
            self.assertEqual(app.load_catalog(path)["products"][0]["sku"], "sample")

    def test_recommendation_api_runs_and_validates_scope(self):
        status, _content_type, raw = self.request("POST", "/api/recommendations", {"scenario": {"supplier": "missing"}})
        payload = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertGreater(payload["metrics"]["recommendation_positions"], 0)
        self.assertTrue(any("Поставщик не найден" in warning for warning in payload["warnings"]))

    def test_invalid_json_and_unknown_route_return_short_errors(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("POST", "/api/recommendations", body=b"{")
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertNotIn("traceback", payload["error"].lower())
        status, _content_type, raw = self.request("GET", "/not-found")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(raw))

    def test_agent_endpoint_keeps_demo_flow_available(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            status, _content_type, raw = self.request("POST", "/api/agent", {"query": "Покажи срочные позиции IEK на 30 дней"})
        payload = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "demo")
        self.assertEqual(payload["intent"], "summarize_replenishment_plan")
        self.assertIn("summarize_replenishment_plan", payload["tool"])
        self.assertTrue(payload["result"]["recommendations"])

    def test_stockout_csv_validation_skips_unknown_sku_and_invalid_days(self):
        sku = app.CATALOG["products"][0]["sku"]
        cleaned, warnings = app.clean_stockouts({sku: {"2026-02": 28, "2026-04": 31}, "unknown": {"2026-08": 1}})
        self.assertEqual(cleaned[sku], {"2026-02": 28})
        self.assertEqual(len(warnings), 2)


if __name__ == "__main__":
    unittest.main()
