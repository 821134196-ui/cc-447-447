"""进程内定时任务：周期性完成读数小时汇总与越限提醒扫描。"""
import threading

from .. import config
from . import readings as reading_svc


class IntervalTask:
    def __init__(self, db_path=None, interval=30.0):
        self.db_path = db_path or config.DB_PATH
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.last_run = None
        self.last_aggregated = 0
        self.last_overlimit = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="interval-task")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def run_once(self, conn):
        self.last_aggregated = reading_svc.aggregate_hourly(conn)
        self.last_overlimit = reading_svc.scan_overlimit(conn)
        from . import timeutil as t
        self.last_run = t.iso_now()
        return self.last_aggregated, self.last_overlimit

    def _run(self):
        from ..db import get_conn
        conn = get_conn(self.db_path)
        try:
            while not self._stop.is_set():
                try:
                    self.run_once(conn)
                except Exception as e:
                    print("[task] error:", e)
                self._stop.wait(self.interval)
        finally:
            conn.close()
