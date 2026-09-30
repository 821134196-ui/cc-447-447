"""越限指令审批测试：越限必须经理批准；运维可执行但不能批准/复核。"""
import unittest

from app.services import curves as curve_svc
from app.services import commands as cmd_svc
from tests.helpers import fresh_db, CURVE, OVER_CURVE

USERS = {  # id -> 最小 user dict
    "zhang": {"id": 1, "role": "dispatcher"},
    "li": {"id": 2, "role": "dispatcher"},
    "wang": {"id": 3, "role": "manager"},
    "sun": {"id": 4, "role": "ops"},
}


class TestOverLimitApproval(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def tearDown(self):
        self.conn.close()

    def _submit(self, bid, points, user_id=1):
        curve_svc.save_curve(self.conn, 1, bid, CURVE, user_id=user_id,
                             base_version=None, note="v1")
        curve_svc.save_curve(self.conn, 1, bid, points, user_id=user_id,
                             base_version=1, note="v2")
        return cmd_svc.submit(self.conn, 1, bid, user_id)

    def test_over_limit_requires_manager(self):
        cmd = self._submit(1, OVER_CURVE)
        self.assertEqual(cmd["status"], "pending_review")
        self.assertEqual(cmd["over_limit"], 1)
        self.assertTrue(cmd["limit_detail"])

        # 运维不能批准越限指令
        with self.assertRaises(PermissionError):
            cmd_svc.review(self.conn, cmd["id"], USERS["sun"], True)
        # 值班员也不能批准越限指令
        with self.assertRaises(PermissionError):
            cmd_svc.review(self.conn, cmd["id"], USERS["zhang"], True)
        # 状态仍为待复核
        self.assertEqual(cmd_svc.get(self.conn, cmd["id"])["status"], "pending_review")

        # 供热经理批准 → 待执行
        cmd = cmd_svc.review(self.conn, cmd["id"], USERS["wang"], True, "经理批准")
        self.assertEqual(cmd["status"], "pending_execution")

        # 运维执行 → 生效观察（运维能执行，但不能批准）
        cmd = cmd_svc.execute(self.conn, cmd["id"], USERS["sun"])
        self.assertEqual(cmd["status"], "observing")
        self.assertIsNotNone(cmd["observe_until"])

    def test_ops_cannot_review_normal_command(self):
        # 正常指令：值班员可复核；运维不能复核
        normal = [dict(p) for p in CURVE]
        cmd = self._submit(2, normal)
        self.assertEqual(cmd["over_limit"], 0)
        with self.assertRaises(PermissionError):
            cmd_svc.review(self.conn, cmd["id"], USERS["sun"], True)
        cmd = cmd_svc.review(self.conn, cmd["id"], USERS["li"], True)
        self.assertEqual(cmd["status"], "pending_execution")

    def test_dispatcher_reject_over_limit(self):
        cmd = self._submit(3, OVER_CURVE)
        # 经理驳回 → rejected 终态
        cmd = cmd_svc.review(self.conn, cmd["id"], USERS["wang"], False, "参数过激")
        self.assertEqual(cmd["status"], "rejected")
        # 被驳回的指令不能执行
        with self.assertRaises(cmd_svc.CommandError):
            cmd_svc.execute(self.conn, cmd["id"], USERS["sun"])

    def test_observe_and_finish_with_cold_complaints(self):
        from datetime import timedelta
        from app.services import timeutil as t
        from app.services import complaints as cmp_svc
        # building 2 使用正常曲线
        curve_svc.save_curve(self.conn, 1, 2, CURVE, user_id=1,
                             base_version=None, note="v1")
        cmd = cmd_svc.submit(self.conn, 1, 2, 1)
        cmd_svc.review(self.conn, cmd["id"], USERS["wang"], True)
        cmd = cmd_svc.execute(self.conn, cmd["id"], USERS["sun"])
        # 观察期内同楼栋 3 单不热报修（1 单登记住户，2 单匿名）
        cmp_svc.create_complaint(self.conn, 1, 2, "cold", "住户不热", resident_id=3)
        cmp_svc.create_complaint(self.conn, 1, 2, "cold", "匿名住户不热")
        cmp_svc.create_complaint(self.conn, 1, 2, "cold", "匿名住户也不热")
        linked = cmd_svc.link_observation_complaints(self.conn, cmd["id"])
        self.assertEqual(linked, 3)
        # 观察期未到不能结束
        with self.assertRaises(cmd_svc.CommandError):
            cmd_svc.finish(self.conn, cmd["id"], USERS["zhang"])
        # 将观察期调到过去后结束 → 效果结论应反映不热
        self.conn.execute("UPDATE commands SET observe_until=? WHERE id=?",
                          (t.fmt(t.now() - timedelta(minutes=1)), cmd["id"]))
        self.conn.commit()
        cmd = cmd_svc.finish(self.conn, cmd["id"], USERS["zhang"])
        self.assertEqual(cmd["status"], "finished")
        self.assertIn("效果不佳", cmd["effect_note"])


if __name__ == "__main__":
    unittest.main()
