"""版本竞争测试：两名值班员基于同一版本提交，后提交者收到冲突提示。"""
import unittest

from app.services import curves as curve_svc
from tests.helpers import fresh_db, CURVE


class TestVersionConflict(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def tearDown(self):
        self.conn.close()

    def test_second_commit_conflicts(self):
        # 初始 v1
        v1, _ = curve_svc.save_curve(
            self.conn, 1, 1, CURVE, user_id=1, base_version=None, note="初始")
        self.assertEqual(v1["version"], 1)
        # 张建国先基于 v1 提交 v2
        zhang_pts = [dict(p, supply=p["supply"] + 2) for p in CURVE]
        v2, _ = curve_svc.save_curve(self.conn, 1, 1, zhang_pts, user_id=1,
                                     base_version=1, note="张建国的修改")
        self.assertEqual(v2["version"], 2)
        # 李雪也基于 v1 提交（未看到 v2）→ 冲突
        li_pts = [dict(p, supply=p["supply"] + 3) for p in CURVE]
        with self.assertRaises(curve_svc.ConflictError) as ctx:
            curve_svc.save_curve(self.conn, 1, 1, li_pts, user_id=2,
                                 base_version=1, note="李雪的修改")
        msg = str(ctx.exception)
        self.assertIn("v2", msg)          # 提示最新版本号
        self.assertIn("张建国", msg)      # 提示是谁先提交
        # 冲突期间数据库里仍只有 v1、v2（没有产生李雪的版本）
        versions = curve_svc.list_versions(self.conn, 1, 1)
        self.assertEqual(len(versions), 2)
        # 李雪合并后基于 v2 重新提交 → 成功
        merged = [dict(p, supply=p["supply"] + 1) for p in li_pts]
        v3, _ = curve_svc.save_curve(self.conn, 1, 1, merged, user_id=2,
                                     base_version=2, note="合并后提交")
        self.assertEqual(v3["version"], 3)

    def test_optimistic_lock_via_http_409(self):
        """经 HTTP 层确认冲突返回 409 与 curve_conflict 标记。"""
        import os
        import json
        import threading
        from http.client import HTTPConnection
        from app import config
        from app.http_api import make_server
        from tests.helpers import fresh_db, _TMP

        class CookieConn(HTTPConnection):
            def __init__(self, host, port):
                super().__init__(host, port)
                self.cookie = None

            def post(self, path, body):
                headers = {"Content-Type": "application/json"}
                if self.cookie:
                    headers["Cookie"] = self.cookie
                self.request("POST", path, json.dumps(body).encode(), headers)
                r = self.getresponse()
                sc = r.getheader("Set-Cookie")
                if sc and "hss_token=" in sc:
                    tok = sc.split("hss_token=", 1)[1].split(";", 1)[0]
                    if tok:
                        self.cookie = f"hss_token={tok}"
                return r

        db_path = os.path.join(_TMP, "http-conflict.db")
        fresh_db(db_path)
        old = config.DB_PATH
        config.DB_PATH = db_path
        srv = make_server("127.0.0.1", 0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        port = srv.server_address[1]
        c1, c2 = CookieConn("127.0.0.1", port), CookieConn("127.0.0.1", port)
        try:
            c1.post("/api/login", {"username": "zhang"})
            c2.post("/api/login", {"username": "li"})
            r0 = c1.post("/api/curve",
                         {"station_id": 1, "building_id": 2, "points": CURVE,
                          "base_version": None, "note": "初始 v1"})
            self.assertEqual(r0.status, 201)
            r1 = c1.post("/api/curve",
                         {"station_id": 1, "building_id": 2,
                          "points": [dict(p, supply=p["supply"] + 2) for p in CURVE],
                          "base_version": 1})
            self.assertEqual(r1.status, 201)
            r2 = c2.post("/api/curve",
                         {"station_id": 1, "building_id": 2,
                          "points": [dict(p, supply=p["supply"] + 3) for p in CURVE],
                          "base_version": 1})
            self.assertEqual(r2.status, 409)
            body = json.loads(r2.read())
            self.assertEqual(body["code"], "curve_conflict")
            self.assertIn("张建国", body["error"])
        finally:
            srv.shutdown()
            srv.server_close()
            config.DB_PATH = old


def _start():
    pass


if __name__ == "__main__":
    unittest.main()
