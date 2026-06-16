"""웹 포탈 API 통합 테스트: 임시 포트로 서버를 띄워 실제로 호출한다.

macOS 명령이 없는 환경에서도 동작하도록, 카운터/식별은 graceful 하게 비어도
API 응답 형태와 동작(시작/중지/저장/조회)을 검증한다.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netusage.config import save_config  # noqa: E402
from netusage.storage import Storage  # noqa: E402
from netusage.webportal import AppState, make_server  # noqa: E402


class TestWebPortal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="netusage_web_")
        cls.db_path = os.path.join(cls.tmp, "test.db")
        cls.cfg_path = os.path.join(cls.tmp, "config.json")
        cls.config = {
            "sample_interval_seconds": 5,
            "wifi_interface": "en0",
            "db_path": cls.db_path,
            "ping_timeout_ms": 1000,
            "named_networks": [{"name": "Home", "ping": "192.168.0.1"}],
        }
        save_config(cls.config, cls.cfg_path)

        # 샘플 시드
        db = Storage(cls.db_path)
        db.insert_sample(1000, "en0", "wifi", "Home", "Home", "192.168.0.1", 500, 200)
        db.insert_sample(1100, "en5", "ethernet", "Office", None, "10.0.0.1", 9000, 100)
        db.close()

        cls.app = AppState(dict(cls.config), cls.cfg_path)
        cls.httpd = make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.app.controller.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    # -- helpers ---------------------------------------------------------
    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path):
        with urllib.request.urlopen(self._url(path), timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def _post(self, path, obj=None):
        data = json.dumps(obj).encode("utf-8") if obj is not None else b""
        req = urllib.request.Request(self._url(path), data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8")

    # -- tests -----------------------------------------------------------
    def test_index_html(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("netusage", body)
        self.assertIn("<textarea id=\"cfg\"", body)

    def test_status(self):
        status, body = self._get("/api/status")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertIn("identity", d)
        self.assertIn("monitor", d)
        self.assertEqual(d["totals"]["total"], 500 + 200 + 9000 + 100)

    def test_report_aggregate(self):
        status, body = self._get("/api/report?by=network")
        self.assertEqual(status, 200)
        d = json.loads(body)
        labels = {r["label"]: r for r in d["rows"]}
        self.assertEqual(labels["Office"]["total_bytes"], 9100)
        self.assertEqual(labels["Home"]["total_bytes"], 700)
        # 합계 큰 순 정렬
        self.assertEqual(d["rows"][0]["label"], "Office")

    def test_report_invalid_by(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/report?by=nope")
        self.assertEqual(ctx.exception.code, 400)

    def test_export_csv(self):
        status, body = self._get("/api/export?format=csv")
        self.assertEqual(status, 200)
        self.assertIn("ts,datetime,iface,conn_type,network", body)
        self.assertIn("Office", body)

    def test_config_get_and_save(self):
        status, body = self._get("/api/config")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["config"]["sample_interval_seconds"], 5)

        # 수정: interval 변경 + named_networks 추가
        new_cfg = dict(d["config"])
        new_cfg["sample_interval_seconds"] = 30
        new_cfg["named_networks"] = [
            {"name": "Home", "ping": "192.168.0.1"},
            {"name": "Office", "gateway": "10.0.0.1"},
        ]
        status, body = self._post("/api/config", new_cfg)
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["config"]["sample_interval_seconds"], 30)

        # 파일에도 반영되었는지
        with open(self.cfg_path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["sample_interval_seconds"], 30)
        # 공유 config dict 도 갱신
        self.assertEqual(self.app.config["sample_interval_seconds"], 30)

    def test_config_save_rejects_bad_named(self):
        bad = dict(self.app.config)
        bad["named_networks"] = [{"ping": "1.2.3.4"}]  # name 없음
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/config", bad)
        self.assertEqual(ctx.exception.code, 400)

    def test_monitor_start_stop(self):
        status, body = self._post("/api/monitor/start")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["running"])

        status, body = self._get("/api/status")
        self.assertTrue(json.loads(body)["monitor"]["running"])

        status, body = self._post("/api/monitor/stop")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["running"])

    def test_sample_endpoint(self):
        # macOS 가 아니면 recorded 는 None 일 수 있으나 200 이어야 한다.
        status, body = self._post("/api/sample")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
