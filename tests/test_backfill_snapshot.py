"""断点补录 + 快照不可覆盖测试。"""
import sqlite3
import unittest
from datetime import timedelta

from app.services import timeutil as t
from app.services import readings as reading_svc
from app.services import snapshots as snap_svc
from tests.helpers import fresh_db


def _normal(conn, ts, bid=1, **kw):
    p = dict(outdoor_temp=-5.0, supply_temp=50.0, return_temp=40.0,
             supply_pressure=0.62, return_pressure=0.28,
             pump_status="running", pump_frequency=38.0)
    p.update(kw)
    return reading_svc.add_reading(conn, 1, bid, ts=ts, source="live", **p)


class TestBackfillAndSnapshot(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.now = t.now()

    def tearDown(self):
        self.conn.close()

    def test_gap_opens_break_backfill_closes(self):
        ts1 = t.fmt(self.now - timedelta(minutes=60))
        ts2 = t.fmt(self.now - timedelta(minutes=40))  # 与上一条间隔 20 分钟 > 12
        _normal(self.conn, ts1)
        _normal(self.conn, ts2)
        open_breaks = self.conn.execute(
            "SELECT * FROM events WHERE kind='sensor_break' AND status='open'"
        ).fetchall()
        self.assertEqual(len(open_breaks), 1)
        ev = open_breaks[0]
        # 运维补录断点窗口中间的一条读数
        bf_ts = t.fmt(self.now - timedelta(minutes=50))
        rid, closed = reading_svc.backfill_reading(
            self.conn, 1, 1, ts=bf_ts,
            outdoor_temp=-5, supply_temp=50, return_temp=40,
            supply_pressure=0.62, return_pressure=0.28,
            pump_status="running", pump_frequency=38)
        self.assertTrue(any(e["id"] == ev["id"] for e in closed))
        after = self.conn.execute(
            "SELECT status, close_note FROM events WHERE id=?", (ev["id"],)).fetchone()
        self.assertEqual(after["status"], "closed")
        self.assertIn("断点补录", after["close_note"])
        # 补录读数带 backfill 标记
        src = self.conn.execute("SELECT source FROM readings WHERE id=?",
                                (rid,)).fetchone()["source"]
        self.assertEqual(src, "backfill")

    def test_snapshot_append_only_triggers(self):
        _normal(self.conn, t.fmt(self.now - timedelta(minutes=5)))
        snap = snap_svc.take_snapshot(self.conn, 1)
        _normal(self.conn, t.fmt(self.now - timedelta(minutes=2)))
        snap2 = snap_svc.take_snapshot(self.conn, 1)
        # 哈希链：第二张快照的 prev_hash 指向第一张
        self.assertEqual(snap2["prev_hash"], snap["hash"])
        # 数据库层禁止 UPDATE / DELETE
        with self.assertRaises(sqlite3.IntegrityError) as e1:
            self.conn.execute("UPDATE snapshots SET supply_temp=99 WHERE id=?",
                              (snap["id"],))
        self.assertIn("不可覆盖", str(e1.exception))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM snapshots WHERE id=?", (snap["id"],))
        self.conn.rollback()
        # 哈希链校验通过
        ok, broken = snap_svc.verify_chain(self.conn)
        self.assertTrue(ok)

    def test_backfill_outside_window_does_not_close(self):
        ts1 = t.fmt(self.now - timedelta(minutes=120))
        ts2 = t.fmt(self.now - timedelta(minutes=100))
        _normal(self.conn, ts1)
        _normal(self.conn, ts2)
        # 在窗口之外补录（更早且不在 from~to 内）
        rid, closed = reading_svc.backfill_reading(
            self.conn, 1, 1, ts=t.fmt(self.now - timedelta(minutes=200)),
            outdoor_temp=-5, supply_temp=50, return_temp=40,
            supply_pressure=0.62, return_pressure=0.28,
            pump_status="running", pump_frequency=38)
        self.assertEqual(closed, [])
        still_open = self.conn.execute(
            "SELECT COUNT(*) n FROM events WHERE kind='sensor_break' AND status='open'"
        ).fetchone()["n"]
        self.assertEqual(still_open, 1)


if __name__ == "__main__":
    unittest.main()
