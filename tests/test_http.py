"""HTTP 端到端测试：角色权限码、版本冲突 409、脱敏、快照与附件。"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

_tmp = tempfile.mkdtemp(prefix="heating_http_")
os.environ["HEATING_DATA"] = _tmp
os.environ["HEATING_DB"] = os.path.join(_tmp, "http.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import seed
from app.server import make_server


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        seed.seed(force=True)
        cls.httpd = make_server("127.0.0.1", 8199)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:8199"
        cls.tokens = {u: cls.login(u) for u in
                      ("dispatcher1", "dispatcher2", "manager1", "ops1", "service1")}

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    @classmethod
    def login(cls, username):
        d = cls.call("/api/login", {"username": username})
        return d["token"]

    @classmethod
    def call(cls, path, body=None, token=None, raw=None, headers=None, method=None):
        url = cls.base + path
        data = None
        hdrs = dict(headers or {})
        if raw is not None:
            data = raw
            hdrs.setdefault("Content-Type", "application/octet-stream")
        elif body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        if token:
            hdrs["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=data, headers=hdrs,
                                     method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(req) as r:
                payload = r.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                detail = json.loads(payload)
            except json.JSONDecodeError:
                detail = {"raw": payload.decode("utf-8", "replace")}
            raise ApiError(e.code, detail) from None

    def post(self, path, body, who):
        return self.call(path, body=body, token=self.tokens[who])

    def setUp(self):
        # 每个用例前重建数据，保持计数干净
        seed.seed(force=True)

    FRESH = [{"outdoor": -10, "supply": 60, "return": 50},
             {"outdoor": 0, "supply": 49, "return": 40},
             {"outdoor": 8, "supply": 43, "return": 36}]
    BREACH = [{"outdoor": -20, "supply": 80, "return": 62},
              {"outdoor": -5, "supply": 55, "return": 46},
              {"outdoor": 5, "supply": 45, "return": 37}]

    def test_version_conflict_409(self):
        self.post("/api/buildings/6/commands",
                  {"points": self.FRESH, "base_version": 1}, "dispatcher1")
        with self.assertRaises(ApiError) as cm:
            self.post("/api/buildings/6/commands",
                      {"points": self.FRESH, "base_version": 1}, "dispatcher2")
        self.assertEqual(cm.exception.code, 409)
        self.assertIn("版本冲突", cm.exception.detail["error"])

    def test_breach_requires_manager_but_ops_executes(self):
        c = self.post("/api/buildings/6/commands",
                      {"points": self.BREACH, "base_version": 1}, "dispatcher2")
        self.assertEqual(c["breach"], 1)
        # 值班员批准越限 → 403
        with self.assertRaises(ApiError) as cm:
            self.post(f"/api/commands/{c['id']}/review",
                      {"approve": True}, "dispatcher1")
        self.assertEqual(cm.exception.code, 403)
        # 运维批准 → 403（运维只能执行）
        with self.assertRaises(ApiError) as cm:
            self.post(f"/api/commands/{c['id']}/review",
                      {"approve": True}, "ops1")
        self.assertEqual(cm.exception.code, 403)
        # 站长批准
        r = self.post(f"/api/commands/{c['id']}/review",
                      {"approve": True, "note": "同意"}, "manager1")
        self.assertEqual(r["status"], "pending_execution")
        # 客服执行 → 403
        with self.assertRaises(ApiError) as cm:
            self.call(f"/api/commands/{c['id']}/execute", body={},
                      token=self.tokens["service1"])
        self.assertEqual(cm.exception.code, 403)
        # 运维执行
        r = self.call(f"/api/commands/{c['id']}/execute", body={},
                      token=self.tokens["ops1"])
        self.assertEqual(r["status"], "observing")
        # 观察期结束
        r = self.post(f"/api/commands/{c['id']}/end",
                      {"judgement": "effective", "note": "恢复正常"}, "dispatcher1")
        self.assertEqual(r["status"], "ended")

    def test_ticket_merge_and_masking_over_http(self):
        t1 = self.post("/api/tickets",
                       {"building_id": 6, "room": "1单元301", "category": "cold",
                        "channel": "电话", "description": "不热"}, "service1")
        t2 = self.post("/api/tickets",
                       {"building_id": 6, "room": "1单元301", "category": "cold",
                        "channel": "小程序", "description": "还是不热"}, "service1")
        self.assertEqual(t2["state"], "merged")
        self.assertEqual(t2["master_id"], t1["id"])
        # 客服看到脱敏
        self.assertTrue(t1["resident_name"].endswith("**"))
        self.assertIn("****", t1["resident_phone"])
        # 值班员看到完整信息
        d = self.call(f"/api/tickets/{t1['id']}", token=self.tokens["dispatcher1"])
        self.assertNotIn("**", d["resident_name"])

    def test_role_separation_backfill_and_event(self):
        # 客服不能补录
        with self.assertRaises(ApiError) as cm:
            self.post("/api/points/1/backfill",
                      {"ts": "2026-09-29T10:00:00+08:00", "value": 12.3}, "service1")
        self.assertEqual(cm.exception.code, 403)
        # 运维可以补录（补录后断点事件自动关闭）
        r = self.post("/api/points/11/backfill",
                      {"ts": "2026-09-29T10:00:00+08:00", "value": 53.1}, "ops1")
        self.assertEqual(r["value"], 53.1)

    def test_snapshot_and_attachment(self):
        # 新开一个班次并拍快照
        sid = 2  # 清河站没有进行中班次
        s = self.post(f"/api/stations/{sid}/shifts-open",
                      {"kind": "day"}, "dispatcher1")
        snap = self.post(f"/api/stations/{sid}/snapshots",
                         {"note": "HTTP 测试快照", "window_minutes": 2880}, "dispatcher1")
        d = self.call(f"/api/snapshots/{snap['id']}", token=self.tokens["dispatcher1"])
        self.assertTrue(len(d["points"]) >= 4)
        # 上传附件
        up = self.call(f"/api/shifts/{s['id']}/attachments",
                       raw="交接内容".encode(), token=self.tokens["dispatcher1"],
                       headers={"X-Filename": "note.txt"})
        self.assertEqual(up["filename"], "note.txt")
        # 下载
        with urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.base}/api/attachments/{up['id']}",
                    headers={"Authorization": "Bearer " + self.tokens["dispatcher1"]})) as r:
            self.assertEqual(r.read().decode(), "交接内容")
        # 交接
        c = self.post(f"/api/stations/{sid}/shifts-close",
                      {"note": "测试交接"}, "dispatcher1")
        self.assertIsNotNone(c["ended_at"])

    def test_station_overview_and_series(self):
        d = self.call("/api/overview?station_id=1", token=self.tokens["dispatcher1"])
        self.assertTrue(any(p["kind"] == "outdoor_temp" for p in d["readings"]))
        pts = self.call("/api/points?station_id=1", token=self.tokens["dispatcher1"])["points"]
        ids = [p["id"] for p in pts[:2]]
        s = self.call("/api/series?points=" + ",".join(map(str, ids)),
                      token=self.tokens["dispatcher1"])
        self.assertEqual(set(s["series"].keys()), set(map(str, ids)))

    def test_unauthenticated_rejected(self):
        with self.assertRaises(ApiError) as cm:
            self.call("/api/overview?station_id=1")
        self.assertEqual(cm.exception.code, 403)


class ApiError(Exception):
    def __init__(self, code, detail):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.detail = detail


if __name__ == "__main__":
    unittest.main(verbosity=2)
