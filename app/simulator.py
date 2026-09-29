"""仪表模拟适配器：按物理模型持续产生读数，并偶发注入异常。

真实时间戳，每个 tick 模拟 5 分钟；读数写入 readings（断点即缺行）。
"""
from __future__ import annotations

import math
import random
import threading
from datetime import datetime

from . import curves
from .db import get_conn, now_iso, query

TICK_MINUTES = 5


class SimAdapter:
    def __init__(self, seed: int | None = None):
        self.rnd = random.Random(seed)
        self._lock = threading.Lock()
        # 处于中断中的测点 point_id -> 剩余 tick
        self.outages: dict[int, int] = {}
        # 异常窗口 point_id -> (剩余tick, 配置)
        self.anomalies: dict[int, tuple[int, dict]] = {}
        self._state: dict[int, float] = {}   # 一阶惯性的实际供水温度
        self._pump: dict[int, float] = {}
        self._inherited = False

    def _inherit_stale_outages(self, conn):
        """启动时延续种子数据里尚未恢复的断点（持续 2 拍后恢复）。"""
        if self._inherited:
            return
        self._inherited = True
        rows = conn.execute(
            """
            SELECT p.id FROM points p
            JOIN (SELECT point_id, MAX(ts) m FROM readings GROUP BY point_id) x
              ON x.point_id=p.id
            WHERE x.m < ?
            """,
            (__import__("datetime").datetime.now().astimezone()
             - __import__("datetime").timedelta(minutes=TICK_MINUTES - 1)
             ).isoformat(timespec="seconds"),
        ).fetchall()
        for r in rows:
            self.outages.setdefault(r["id"], 2)

    # -------- 物理模型 --------
    def outdoor_at(self, station_id: int, t: datetime) -> float:
        # 日变化：凌晨 5 点最冷，午后最暖
        h = t.hour + t.minute / 60
        phase = (h - 9) / 24 * 2 * math.pi
        base = -7 + 0.9 * station_id
        return base - 5.5 * math.cos(phase) + self.rnd.uniform(-0.4, 0.4)

    def _active_curve(self, conn, building_id: int):
        row = conn.execute(
            "SELECT points_json FROM curve_versions WHERE building_id=? "
            "AND status='active' ORDER BY version_no DESC LIMIT 1",
            (building_id,),
        ).fetchone()
        if row:
            import json
            return json.loads(row["points_json"])
        return None

    def tick(self):
        """推进一个模拟步，写入当前时刻各测点读数。"""
        with self._lock:
            conn = get_conn()
            try:
                self._inherit_stale_outages(conn)
                ts = now_iso()
                t = datetime.now().astimezone()
                stations = conn.execute("SELECT id FROM stations").fetchall()
                for st in stations:
                    sid = st["id"]
                    outdoor = self.outdoor_at(sid, t)
                    points = {p["kind"]: p for p in conn.execute(
                        "SELECT * FROM points WHERE station_id=? AND building_id IS NULL",
                        (sid,)).fetchall()}
                    self._emit(conn, points.get("outdoor_temp")["id"], ts, outdoor)

                    # 压力（偶发骤降）
                    sp = points.get("supply_pressure")
                    rp = points.get("return_pressure")
                    pump = points.get("pump_status")
                    self._maybe_inject(sid, sp, pump)
                    drop = self.anomalies.get(sp["id"], (0, {}))[1].get("drop", 0) if sp else 0
                    if sp:
                        self._emit(conn, sp["id"], ts,
                                   0.56 + self.rnd.uniform(-0.015, 0.015) - drop)
                    if rp:
                        self._emit(conn, rp["id"], ts,
                                   0.29 + self.rnd.uniform(-0.01, 0.01) - drop * 0.4)
                    if pump:
                        target = self._pump.setdefault(sid, 1.0)
                        sw = self.anomalies.get(pump["id"], (0, {}))[1].get("pump")
                        if sw is not None:
                            self._pump[sid] = sw
                        val = sw if sw is not None else target
                        self._emit(conn, pump["id"], ts, val)

                    # 楼栋供回水温度跟随生效曲线
                    for b in conn.execute(
                        "SELECT id FROM buildings WHERE station_id=?", (sid,)
                    ).fetchall():
                        bid = b["id"]
                        bp = {p["kind"]: p for p in conn.execute(
                            "SELECT * FROM points WHERE building_id=?", (bid,)).fetchall()}
                        sup_p, ret_p = bp.get("supply_temp"), bp.get("return_temp")
                        if not sup_p or not ret_p:
                            continue
                        cpts = self._active_curve(conn, bid)
                        target = curves.interpolate(cpts, outdoor, "supply") if cpts else 55
                        key = sup_p["id"]
                        actual = self._state.get(key, target)
                        actual += (target - actual) * 0.35 + self.rnd.uniform(-0.5, 0.5)
                        self._state[key] = actual
                        delta_bias = self.anomalies.get(ret_p["id"], (0, {}))[1].get("delta", 0)
                        load = max(0.0, -outdoor) * 0.25
                        delta = 9.5 + load + self.rnd.uniform(-0.8, 0.8) + delta_bias
                        self._emit(conn, sup_p["id"], ts, actual)
                        self._emit(conn, ret_p["id"], ts, actual - delta)

                self._decay()
                conn.commit()
            finally:
                conn.close()

    def _emit(self, conn, point_id, ts, value):
        if point_id is None:
            return
        remaining = self.outages.get(point_id, 0)
        if remaining > 0:
            return  # 断点：不写读数
        conn.execute(
            "INSERT OR IGNORE INTO readings(point_id, ts, value, backfilled) VALUES(?,?,?,0)",
            (point_id, ts, round(float(value), 3)),
        )

    def _maybe_inject(self, station_id, sp, pump):
        r = self.rnd.random()
        if sp and r < 0.03 and sp["id"] not in self.anomalies:
            # 压力骤降持续约 90 秒（后台任务每 60 秒扫描一次）
            self.anomalies[sp["id"]] = (6, {"drop": 0.13})
        elif r < 0.06:
            # 温差异常：回水抬升导致温差过小（随机楼栋回水点，持续约 3 分钟）
            conn = get_conn()
            try:
                row = conn.execute(
                    "SELECT id FROM points WHERE station_id=? AND kind='return_temp' "
                    "ORDER BY RANDOM() LIMIT 1", (station_id,)).fetchone()
            finally:
                conn.close()
            if row and row["id"] not in self.anomalies:
                self.anomalies[row["id"]] = (12, {"delta": -6.0})
        elif pump and r < 0.09 and pump["id"] not in self.anomalies:
            other = 2.0 if self._pump.get(station_id, 1.0) == 1.0 else 1.0
            self.anomalies[pump["id"]] = (5, {"pump": other})
        # 随机断点：持续 5~8 分钟，足够触发断点事件
        if r < 0.04:
            conn = get_conn()
            try:
                row = conn.execute(
                    "SELECT id FROM points ORDER BY RANDOM() LIMIT 1").fetchone()
            finally:
                conn.close()
            if row and row["id"] not in self.outages:
                self.outages[row["id"]] = self.rnd.randint(20, 32)

    def _decay(self):
        for pid in list(self.outages):
            self.outages[pid] -= 1
            if self.outages[pid] <= 0:
                del self.outages[pid]
        for pid, (n, cfg) in list(self.anomalies.items()):
            if n <= 1:
                del self.anomalies[pid]
            else:
                self.anomalies[pid] = (n - 1, cfg)
