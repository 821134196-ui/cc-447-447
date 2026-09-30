"""数据权限测试（经完整 HTTP 栈）：

- 客服只能看到脱敏住户信息，且不能访问工艺数据（趋势/曲线）与指令操作；
- 运维不能批准/复核指令，但可以执行；
- 未登录访问受保护接口返回 401；
- 越限指令的批准仅经理可用。
"""
import json
import os
import threading
import unittest
from http.client import HTTPConnection

from app import config
from app.http_api import make_server
from tests.helpers import fresh_db, _TMP, CURVE, OVER_CURVE


class HttpSession:
    def __init__(self, base):
        self.c = HTTPConnection(*base)
        self.cookie = None

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        self.c.request(method, path, data, headers)
        r = self.c.getresponse()
        raw = r.read()
        sc = r.getheader("Set-Cookie")
        if sc and "hss_token=" in sc:
            tok = sc.split("hss_token=", 1)[1].split(";", 1)[0]
            if tok:
                self.cookie = f"hss_token={tok}"
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return r.status, parsed

    def login(self, username):
        return self.call("POST", "/api/login", {"username": username})


class TestDataPermissions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.join(_TMP, "http-perm.db")
        conn = fresh_db(cls.db_path)
        # 预置：1 号楼正常曲线 + 2 号楼越限曲线及待复核指令
        from app.services import curves as curve_svc, commands as cmd_svc
        from app.services import complaints as cmp_svc
        curve_svc.save_curve(conn, 1, 1, CURVE, user_id=1,
                             base_version=None, note="v1")
        curve_svc.save_curve(conn, 1, 2, CURVE, user_id=1,
                             base_version=None, note="v1")
        curve_svc.save_curve(conn, 1, 2, OVER_CURVE, user_id=1,
                             base_version=1, note="越限v2")
        cls.over_cmd = cmd_svc.submit(conn, 1, 2, 1)
        # 一条带住户的报修，供脱敏断言
        cls.complaint, _, _ = cmp_svc.create_complaint(
            conn, 1, 1, "cold", "不热", resident_id=1)
        conn.close()

        cls.old_db = config.DB_PATH
        config.DB_PATH = cls.db_path
        cls.srv = make_server("127.0.0.1", 0)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = ("127.0.0.1", cls.srv.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        config.DB_PATH = cls.old_db

    def new(self):
        return HttpSession(self.base)

    # ---- 未登录 ----
    def test_00_anonymous_rejected(self):
        s = self.new()
        for path in ("/api/overview", "/api/complaints", "/api/trend", "/api/events"):
            status, _ = s.call("GET", path)
            self.assertEqual(status, 401, path)

    # ---- 客服 ----
    def test_10_service_masked_residents(self):
        s = self.new()
        self.assertEqual(s.login("zhou")[0], 200)
        status, d = s.call("GET", "/api/complaints?station_id=1")
        self.assertEqual(status, 200)
        row = next(c for c in d["complaints"] if c["id"] == self.complaint["id"])
        # 住户1 姓名"住户1"→"住*"，电话 13800000001 → 138****0001，房号首字+***
        self.assertIn("*", row["resident_name"])
        self.assertNotEqual(row["resident_name"], "住户1")
        self.assertTrue(row["resident_phone"].startswith("138****"))
        self.assertIn("***", row["room"])

    def test_11_service_no_process_data(self):
        s = self.new()
        s.login("zhou")
        # 工艺数据不可见
        self.assertEqual(s.call("GET", "/api/trend?buildings=1")[0], 403)
        self.assertEqual(s.call("GET", "/api/curve?building_id=1")[0], 403)
        self.assertEqual(s.call("GET", "/api/readings?building_id=1")[0], 403)
        # 不能编辑曲线 / 下达 / 执行 / 批准
        self.assertEqual(s.call("POST", "/api/curve",
                                {"station_id": 1, "building_id": 1,
                                 "points": CURVE, "base_version": 1})[0], 403)
        self.assertEqual(s.call("POST", "/api/commands",
                                {"station_id": 1, "building_id": 1})[0], 403)
        self.assertEqual(s.call("POST",
                                f"/api/commands/{self.over_cmd['id']}/review",
                                {"approve": True})[0], 403)
        # 但报修列表可见、可登记
        self.assertEqual(s.call("GET", "/api/events")[0], 200)
        st, _ = s.call("POST", "/api/complaints",
                       {"station_id": 1, "building_id": 3,
                        "kind": "cold", "content": "客服登记"})
        self.assertEqual(st, 201)

    # ---- 运维 ----
    def test_20_ops_cannot_approve_but_can_execute(self):
        ops = self.new()
        ops.login("sun")
        # 越限指令：运维复核/批准被拒
        st, d = ops.call("POST", f"/api/commands/{self.over_cmd['id']}/review",
                         {"approve": True})
        self.assertEqual(st, 403)
        # 值班员也不能批准越限
        disp = self.new()
        disp.login("zhang")
        st, _ = disp.call("POST", f"/api/commands/{self.over_cmd['id']}/review",
                          {"approve": True})
        self.assertEqual(st, 403)
        # 经理批准
        mgr = self.new()
        mgr.login("wang")
        st, d = mgr.call("POST", f"/api/commands/{self.over_cmd['id']}/review",
                         {"approve": True, "note": "经理批准"})
        self.assertEqual(st, 200)
        self.assertEqual(d["command"]["status"], "pending_execution")
        # 运维执行
        st, d = ops.call("POST", f"/api/commands/{self.over_cmd['id']}/execute", {})
        self.assertEqual(st, 200)
        self.assertEqual(d["command"]["status"], "observing")

    def test_21_ops_backfill_allowed(self):
        ops = self.new()
        ops.login("sun")
        st, _ = ops.call("POST", "/api/readings/backfill", {
            "station_id": 1, "building_id": 1,
            "ts": "2026-01-01 00:00:00",
            "outdoor_temp": -5, "supply_temp": 50, "return_temp": 40,
            "supply_pressure": 0.62, "return_pressure": 0.28})
        self.assertEqual(st, 200)

    # ---- 值班员 ----
    def test_30_dispatcher_cannot_execute(self):
        s = self.new()
        s.login("li")
        # 再构造一条待执行的正常指令
        st, d = s.call("POST", "/api/commands",
                       {"station_id": 1, "building_id": 1})
        self.assertEqual(st, 201)
        cid = d["command"]["id"]
        mgr = self.new()
        mgr.login("wang")
        mgr.call("POST", f"/api/commands/{cid}/review", {"approve": True})
        # 值班员执行 → 403（执行权归运维）
        self.assertEqual(s.call("POST", f"/api/commands/{cid}/execute", {})[0], 403)
        ops = self.new()
        ops.login("sun")
        self.assertEqual(ops.call("POST", f"/api/commands/{cid}/execute", {})[0], 200)


if __name__ == "__main__":
    unittest.main()
