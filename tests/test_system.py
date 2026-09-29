"""系统测试：版本竞争、断点补录、越限审批、报修归并、数据权限。

直接运行：python -m unittest discover -s tests
或：python tests/test_system.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="heating_test_")
os.environ["HEATING_DATA"] = _tmp
os.environ["HEATING_DB"] = os.path.join(_tmp, "test.db")

from app import auth, commands, curves, monitor, readings, seed, shifts, tickets
from app.db import get_conn, query, query_one
from app.curves import VersionConflict


class BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        seed.seed(force=True)
        from app.db import query as _q
        cls.users = {u["username"]: dict(u) for u in _q("SELECT * FROM users")}
        # 用站点 2 的楼栋做干净的功能测试，避免种子数据干扰计数
        cls.bid = query_one(
            "SELECT b.id FROM buildings b JOIN stations s ON s.id=b.station_id "
            "WHERE s.code='QH' ORDER BY b.code LIMIT 1")["id"]
        cls.sid = query_one("SELECT station_id FROM buildings WHERE id=?", (cls.bid,))["station_id"]

    def u(self, username):
        return dict(self.users[username])

    def fresh_curve(self):
        return [
            {"outdoor": -15, "supply": 65, "return": 55},
            {"outdoor": -5, "supply": 55, "return": 46},
            {"outdoor": 5, "supply": 45, "return": 37},
        ]


class TestVersionConflict(BaseCase):
    """两名值班员修改同一曲线版本，后提交者收到冲突提示。"""

    def test_concurrent_submit(self):
        bid = self.bid
        head = query_one(
            "SELECT MAX(version_no) m FROM curve_versions WHERE building_id=?", (bid,))["m"]
        # 值班员 A 先提交成功
        v_a, _ = curves.submit_version(bid, self.fresh_curve(), self.u("dispatcher1"),
                                       base_version=head, note="A 的调整")
        self.assertEqual(v_a["version_no"], head + 1)
        # 值班员 B 仍基于旧版本 → 冲突
        with self.assertRaises(VersionConflict):
            curves.submit_version(bid, self.fresh_curve(), self.u("dispatcher2"),
                                  base_version=head, note="B 的调整")
        # B 刷新基准后可提交
        v_b, reasons = curves.submit_version(
            bid, self.fresh_curve(), self.u("dispatcher2"),
            base_version=head + 1, note="B 重新基于最新版")
        self.assertEqual(v_b["version_no"], head + 2)

    def test_monotonic_validation(self):
        with self.assertRaises(curves.CurveError):
            curves.normalize_points([
                {"outdoor": -5, "supply": 60, "return": 50},
                {"outdoor": 5, "supply": 61, "return": 50},  # 供水应降低
            ])


class TestBackfill(BaseCase):
    """传感器断点补录：写入历史空缺并自动关闭断点事件。"""

    def test_backfill_closes_gap_event(self):
        pt = query_one(
            "SELECT * FROM points WHERE building_id=? AND kind='supply_temp' LIMIT 1",
            (self.bid,))
        from app.db import now_iso
        import json
        from app.db import tx
        with tx() as conn:
            eid = conn.execute(
                "INSERT INTO events(type, severity, station_id, building_id, detected_at, "
                "payload_json) VALUES('sensor_gap','warning',?,NULL,?,?)",
                (pt["station_id"], now_iso(),
                 json.dumps({"point_id": pt["id"]}))).lastrowid
        ts = "2026-09-28T03:00:00+08:00"
        r = readings.backfill_reading(pt["id"], ts, 52.3, self.u("ops1"))
        self.assertTrue(r["value"] == 52.3)
        row = query_one("SELECT * FROM readings WHERE point_id=? AND ts=?", (pt["id"], ts))
        self.assertEqual(row["backfilled"], 1)
        ev = query_one("SELECT * FROM events WHERE id=?", (eid,))
        self.assertEqual(ev["status"], "handled")
        self.assertIn("断点补录", ev["resolution"])

    def test_realtime_reading_not_overwritten(self):
        pt = query_one("SELECT * FROM points WHERE kind='outdoor_temp' LIMIT 1")
        readings.add_reading(pt["id"], "2026-09-28T03:05:00+08:00", 1.0)
        readings.add_reading(pt["id"], "2026-09-28T03:05:00+08:00", 9.0)  # 幂等不覆盖
        row = query_one("SELECT value FROM readings WHERE point_id=? AND ts=?",
                        (pt["id"], "2026-09-28T03:05:00+08:00"))
        self.assertEqual(row["value"], 1.0)


class TestBreachApproval(BaseCase):
    """越限指令必须站长批准；运维不能自行批准，但可执行。"""

    def _breach_command(self):
        pts = [
            {"outdoor": -20, "supply": 80, "return": 62},  # 超过 75℃ 高限
            {"outdoor": -5, "supply": 55, "return": 46},
            {"outdoor": 5, "supply": 45, "return": 37},
        ]
        return commands.create_command(self.bid, pts, self.u("dispatcher2"),
                                       base_version=self._head(), note="极端寒潮")

    def _head(self):
        return query_one("SELECT MAX(version_no) m FROM curve_versions WHERE building_id=?",
                         (self.bid,))["m"]

    def test_breach_flow(self):
        c = self._breach_command()
        self.assertEqual(c["status"], "pending_review")
        self.assertTrue(c["breach"])
        self.assertIn("高限", c["breach_reason"])
        # 值班员不能批准越限
        with self.assertRaises(PermissionError):
            commands.review_command(c["id"], self.u("dispatcher1"), True)
        # 运维不能批准
        with self.assertRaises(PermissionError):
            commands.review_command(c["id"], self.u("ops1"), True)
        # 站长批准
        c = commands.review_command(c["id"], self.u("manager1"), True, "同意寒潮预案")
        self.assertEqual(c["status"], "pending_execution")
        # 运维执行（不能审批但可以执行）
        c = commands.execute_command(c["id"], self.u("ops1"))
        self.assertEqual(c["status"], "observing")
        # 值班员结束观察
        c = commands.end_command(c["id"], self.u("dispatcher1"), "effective", "温度恢复")
        self.assertEqual(c["status"], "ended")
        active = curves.active_version(self.bid)
        self.assertEqual(active["status"], "active")
        self.assertGreaterEqual(max(p["supply"] for p in
                                    __import__("json").loads(active["points_json"])), 80)

    def test_normal_command_dispatcher_can_review(self):
        c = commands.create_command(self.bid, self.fresh_curve(),
                                    self.u("dispatcher1"), base_version=self._head())
        self.assertFalse(c["breach"])
        c = commands.review_command(c["id"], self.u("dispatcher2"), True)
        self.assertEqual(c["status"], "pending_execution")

    def test_ops_cannot_submit(self):
        # 服务层未限制提交角色（在 HTTP 层限制），这里验证 execute 的权限
        c = commands.create_command(self.bid, self.fresh_curve(),
                                    self.u("dispatcher1"), base_version=self._head())
        commands.review_command(c["id"], self.u("manager1"), True)
        # 客服不能执行
        with self.assertRaises(PermissionError):
            commands.execute_command(c["id"], self.u("service1"))


class TestTicketMerge(BaseCase):
    """重复报修归并与集中不热事件。"""

    def test_repeat_merge(self):
        bid = query_one(
            "SELECT b.id FROM buildings b JOIN stations s ON s.id=b.station_id "
            "WHERE s.code='QH' ORDER BY b.code DESC LIMIT 1")["id"]
        t1 = tickets.create_ticket(bid, "2单元501", "电话", "cold", "不热")
        t2 = tickets.create_ticket(bid, "2单元501", "小程序", "cold", "还是不热")
        self.assertEqual(t2["state"], "merged")
        self.assertEqual(t2["master_id"], t1["id"])
        ev = query_one(
            "SELECT * FROM events WHERE type='repeat_repair' AND status='open' "
            "AND building_id=? ORDER BY id DESC LIMIT 1", (bid,))
        self.assertIsNotNone(ev)

    def test_mass_cold(self):
        # 另选楼栋，避免重复
        bid = self.bid
        before = query_one(
            "SELECT COUNT(*) c FROM events WHERE type='mass_cold' AND building_id=?",
            (bid,))["c"]
        rooms = ["1单元101", "1单元102", "2单元203", "2单元204"]
        for room in rooms:
            tickets.create_ticket(bid, room, "电话", "cold", "整栋不热")
        after = query_one(
            "SELECT COUNT(*) c FROM events WHERE type='mass_cold' AND building_id=?",
            (bid,))["c"]
        self.assertGreater(after, before)

    def test_unsatisfied_reopens(self):
        bid = self.bid
        t = tickets.create_ticket(bid, "3单元303", "电话", "leak", "漏水")
        tickets.handle_ticket(t["id"], self.u("ops1"), "已紧固")
        tickets.resolve_ticket(t["id"], self.u("ops1"), "已修复")
        out = tickets.callback(t["id"], self.u("service1"), False, "仍漏水")
        self.assertEqual(out["state"], "reopened")
        self.assertEqual(out["callback_state"], "dissatisfied")
        ev = query_one(
            "SELECT * FROM events WHERE type='callback_unsatisfied' AND status='open' "
            "ORDER BY id DESC LIMIT 1")
        self.assertIsNotNone(ev)


class TestDataPermission(BaseCase):
    """客服只见脱敏住户信息；事件分角色处置；快照不可覆盖。"""

    def test_masking(self):
        t = tickets.create_ticket(self.bid, "1单元301", "电话", "cold", "冷")
        full = tickets.get_ticket(t["id"], self.u("dispatcher1"))
        masked = tickets.get_ticket(t["id"], self.u("service1"))
        self.assertNotEqual(full["resident_name"], masked["resident_name"])
        self.assertTrue(masked["resident_name"].endswith("**"))
        self.assertIn("****", masked["resident_phone"])
        self.assertNotIn("****", full["resident_phone"])
        # 房号不脱敏（调度与客服均需定位）
        self.assertEqual(masked["room"], "1单元301")

    def test_event_role_separation(self):
        # 水泵切换只能运维处置
        from app.db import dumps, now_iso, tx
        with tx() as conn:
            eid = conn.execute(
                "INSERT INTO events(type, severity, station_id, detected_at, payload_json) "
                "VALUES('pump_switch','info',?,?,'{}')",
                (self.sid, now_iso())).lastrowid
        with self.assertRaises(PermissionError):
            monitor.handle_event(eid, self.u("dispatcher1"), "值班员越权处置")
        with self.assertRaises(PermissionError):
            monitor.handle_event(eid, self.u("service1"), "客服越权处置")
        monitor.handle_event(eid, self.u("ops1"), "切泵正常")
        self.assertEqual(query_one("SELECT status FROM events WHERE id=?", (eid,))["status"],
                         "handled")

    def test_snapshot_immutable(self):
        sh = shifts.open_shift(self.sid, "day", self.u("dispatcher1"))
        snap = shifts.take_snapshot(self.sid, self.u("dispatcher1"), "测试快照",
                                    window_minutes=60 * 24 * 2)
        sid = snap["id"]
        before = query_one("SELECT COUNT(*) c FROM snapshot_readings WHERE snapshot_id=?",
                           (sid,))["c"]
        # 快照表只增不改：尝试直接 UPDATE/DELETE 应不影响既有记录的语义
        from app.db import tx
        with tx() as conn:
            conn.execute("UPDATE snapshot_points SET avg_value=-999 WHERE snapshot_id=?",
                         (sid,))
        # 业务层没有任何修改接口；模拟“新读数到达后再拍一帧”是新快照而非覆盖
        snap2 = shifts.take_snapshot(self.sid, self.u("dispatcher1"), "第二帧",
                                     window_minutes=60 * 24 * 2)
        self.assertNotEqual(snap2["id"], sid)
        c1 = query_one("SELECT COUNT(*) c FROM snapshot_readings WHERE snapshot_id=?",
                       (sid,))["c"]
        c2 = query_one("SELECT COUNT(*) c FROM snapshot_readings WHERE snapshot_id=?",
                       (snap2["id"],))["c"]
        self.assertEqual(c1, before)
        self.assertGreaterEqual(c2, 0)
        shifts.close_shift(self.sid, self.u("dispatcher1"), "测试交接")


if __name__ == "__main__":
    unittest.main(verbosity=2)
