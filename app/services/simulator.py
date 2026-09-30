"""模拟适配器：后台线程持续产生仪表读数，并偶发各类运行异常。

线程为守护线程；tick 间隔取 config.SIM_TICK_SECONDS。
每个站点维护室外温度昼夜相位，楼栋读数在目标曲线附近叠加噪声。
"""
import math
import threading
import time

from .. import config
from . import timeutil as t
from . import readings as reading_svc
from . import curves as curve_svc


def _effective_curve(conn, station_id, building_id):
    """实际供水跟随最近已在现场执行的曲线版本（生效观察或已结束的指令）；
    待复核/待执行的草稿曲线不影响实际参数。无则回退到首个版本。"""
    row = conn.execute(
        """SELECT cv.* FROM curve_versions cv
           JOIN commands cmd ON cmd.new_curve_version=cv.version
             AND cmd.building_id=cv.building_id
             AND cmd.status IN ('observing','finished')
           WHERE cv.building_id=?
           ORDER BY cv.version DESC LIMIT 1""", (building_id,)).fetchone()
    if row:
        from ..db import row_to_dict
        return row_to_dict(row)["points"]
    first = conn.execute(
        """SELECT points_json FROM curve_versions
           WHERE station_id=? AND building_id=? ORDER BY version LIMIT 1""",
        (station_id, building_id)).fetchone()
    if first:
        import json
        return json.loads(first["points_json"])
    return None


class SimAdapter:
    def __init__(self, db_path=None):
        self.db_path = db_path or config.DB_PATH
        self._stop = threading.Event()
        self._thread = None
        self._phase = 0.0          # 温度昼夜相位
        self._tick_no = 0
        self._force = {}          # building_id -> 临时异常模式，持续若干 tick

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="sim-adapter")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def inject(self, building_id, mode, ticks=None):
        """测试/演示用：注入异常模式（break/diff/drop/switch/cold）。

        break 默认跳过足够多的 tick（约 13 分钟）以超过断点判定阈值。"""
        if ticks is None:
            ticks = 160 if mode == "break" else 12
        self._force[building_id] = [mode, ticks]

    def _run(self):
        from ..db import get_conn
        conn = get_conn(self.db_path)
        try:
            while not self._stop.is_set():
                try:
                    self._tick(conn)
                except Exception as e:  # 守护线程不能因单次错误退出
                    print("[sim] tick error:", e)
                self._stop.wait(config.SIM_TICK_SECONDS)
        finally:
            conn.close()

    def _tick(self, conn):
        self._tick_no += 1
        self._phase = (self._phase + 0.08) % (2 * math.pi)
        # 室外温度：-8℃ 基线 + 昼夜摆动
        outdoor = round(-8 + 6 * math.sin(self._phase - 1.2), 1)
        buildings = conn.execute(
            "SELECT id, station_id FROM buildings ORDER BY id").fetchall()
        for b in buildings:
            mode = None
            f = self._force.get(b["id"])
            if f:
                mode = f[0]
                f[1] -= 1
                if f[1] <= 0:
                    self._force.pop(b["id"], None)

            cur = _effective_curve(conn, b["station_id"], b["id"])
            if cur:
                ts_, tr_ = curve_svc.interpolate(cur, outdoor)
            else:
                ts_, tr_ = 52.0, 42.0
            # 正常噪声
            sup = ts_ + 0.6 * math.sin(self._phase + b["id"])
            ret = tr_ + 0.5 * math.cos(self._phase + b["id"])
            sp, rp = 0.62, 0.28
            pump_status, freq = "running", 38.0

            if mode == "break":
                # 跳过写入 → 下个 tick 正常时自然形成断点
                continue
            if mode == "diff":
                ret = sup - 3.0
            elif mode == "drop":
                sp = 0.40
            elif mode == "switch":
                pump_status, freq = "switching", 24.0
            elif mode == "cold":
                sup -= 9.0
                ret -= 7.0

            reading_svc.add_reading(
                conn, b["station_id"], b["id"],
                outdoor_temp=outdoor,
                supply_temp=round(sup, 2), return_temp=round(ret, 2),
                supply_pressure=round(sp + 0.01 * math.sin(self._phase), 3),
                return_pressure=round(rp, 3),
                pump_status=pump_status, pump_frequency=freq,
                source="sim")
