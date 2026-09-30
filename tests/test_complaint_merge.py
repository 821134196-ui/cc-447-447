"""报修归并测试：重复报修自动归并；集中不热跨楼栋升级；不满意回访独立事件。"""
import unittest
from datetime import timedelta

from app.services import timeutil as t
from app.services import complaints as cmp_svc
from tests.helpers import fresh_db


class TestComplaintMerge(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.at = t.iso_now()

    def tearDown(self):
        self.conn.close()

    def _at(self, minutes_ago):
        return t.fmt(t.now() - timedelta(minutes=minutes_ago))

    def test_repeat_complaint_merged(self):
        c1, merged, mass = cmp_svc.create_complaint(
            self.conn, 1, 1, "cold", "暖气不热", resident_id=1,
            created_at=self._at(120))
        self.assertIsNone(merged)
        # 同住户同楼栋 24h 内再次报修 → 归并
        c2, target, _ = cmp_svc.create_complaint(
            self.conn, 1, 1, "cold", "还是不热", resident_id=1,
            created_at=self._at(30))
        self.assertEqual(c2["status"], "merged")
        self.assertEqual(c2["merged_into"], c1["id"])
        self.assertEqual(target["id"], c1["id"])
        # 主单内容被追加追访信息
        root = self.conn.execute("SELECT content FROM complaints WHERE id=?",
                                 (c1["id"],)).fetchone()["content"]
        self.assertIn("还是不热", root)
        # 产生独立的「重复报修」事件
        ev = self.conn.execute(
            "SELECT * FROM events WHERE kind='repeat_repair' AND status='open'"
        ).fetchone()
        self.assertIsNotNone(ev)
        self.assertEqual(ev["related_id"], c1["id"])
        # 第三次报修继续归并到同一主单，事件计数累加而非重复开单
        c3, _, _ = cmp_svc.create_complaint(
            self.conn, 1, 1, "cold", "第三次催", resident_id=1)
        self.assertEqual(c3["merged_into"], c1["id"])
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM events WHERE kind='repeat_repair' AND status='open'"
        ).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_different_resident_not_merged(self):
        cmp_svc.create_complaint(self.conn, 1, 1, "cold", "101 不热",
                                 resident_id=1, created_at=self._at(10))
        c, merged, _ = cmp_svc.create_complaint(
            self.conn, 1, 1, "cold", "102 不热", resident_id=2)
        self.assertIsNone(merged)
        self.assertEqual(c["status"], "open")

    def test_mass_cold_cross_buildings(self):
        # 60 分钟内 3 栋不同楼栋报不热 → 集中不热事件
        cmp_svc.create_complaint(self.conn, 1, 1, "cold", "1栋不热",
                                 resident_id=1, created_at=self._at(40))
        cmp_svc.create_complaint(self.conn, 1, 2, "cold", "2栋不热",
                                 resident_id=3, created_at=self._at(20))
        _, _, mass = cmp_svc.create_complaint(
            self.conn, 1, 3, "cold", "3栋不热", created_at=self._at(5))
        self.assertTrue(mass)
        ev = self.conn.execute(
            "SELECT * FROM events WHERE kind='mass_cold' AND status='open'"
        ).fetchone()
        self.assertIsNotNone(ev)
        # 重复的集中不热窗口不重复开单
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM events WHERE kind='mass_cold'"
        ).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_callback_unsatisfied_separate_event(self):
        c, _, _ = cmp_svc.create_complaint(
            self.conn, 1, 3, "leak", "漏水", resident_id=4,
            created_at=self._at(20))
        cmp_svc.resolve(self.conn, c["id"], {"id": 4})
        cmp_svc.add_callback(self.conn, c["id"], "unsatisfied", "仍渗水",
                             {"id": 5})
        ev = self.conn.execute(
            "SELECT * FROM events WHERE kind='callback_unsatisfied' AND status='open'"
        ).fetchone()
        self.assertIsNotNone(ev)
        self.assertEqual(ev["related_id"], c["id"])
        # 满意回访不产生事件
        c2, _, _ = cmp_svc.create_complaint(self.conn, 1, 3, "noise", "异响")
        cmp_svc.resolve(self.conn, c2["id"], {"id": 4})
        cmp_svc.add_callback(self.conn, c2["id"], "satisfied", "好了",
                             {"id": 5})
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM events WHERE kind='callback_unsatisfied'"
        ).fetchone()["n"]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
