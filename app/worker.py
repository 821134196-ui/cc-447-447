"""进程内后台任务。

- 每 15 秒推进一次模拟适配器（每拍模拟 5 分钟数据）
- 每 60 秒：事件检测、小时汇总、观察窗关联与到期提醒
- 越限指令在提交时已生成 notification，这里补发"长期未处理"提醒
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from . import commands, monitor
from .db import get_conn, now_iso
from .readings import hourly_rollup
from .simulator import SimAdapter


class BackgroundWorker(threading.Thread):
    def __init__(self, adapter: SimAdapter, tick_interval: float = 15.0):
        super().__init__(daemon=True, name="heating-worker")
        self.adapter = adapter
        self.tick_interval = tick_interval
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        last_slow = 0.0
        while not self._stop.is_set():
            try:
                self.adapter.tick()
            except Exception as exc:  # 后台任务不应使进程退出
                print("[worker] tick error:", exc)
            now = time.time()
            if now - last_slow >= 60:
                last_slow = now
                try:
                    self._slow_tasks()
                except Exception as exc:
                    print("[worker] slow task error:", exc)
            self._stop.wait(self.tick_interval)

    def _slow_tasks(self):
        monitor.detect(notify=True)
        conn = get_conn()
        try:
            hourly_rollup(conn)
            due = commands.auto_tick(conn)
            for cid in due:
                exists = conn.execute(
                    "SELECT 1 FROM notifications WHERE kind='observe_due' AND ref_id=?",
                    (cid,),
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO notifications(kind, ref_type, ref_id, message, created_at) "
                        "VALUES('observe_due','command',?,?,?)",
                        (cid, f"指令 #{cid} 观察窗已到期，请依据关联报修判定效果并结束",
                         now_iso()),
                    )
            # 越限指令滞留 30 分钟未批准的提醒（幂等：每指令一条）
            since = (datetime.now().astimezone()
                     - timedelta(minutes=30)).isoformat(timespec="seconds")
            rows = conn.execute(
                "SELECT id FROM commands WHERE status='pending_review' AND breach=1 "
                "AND submitted_at<=?", (since,),
            ).fetchall()
            for r in rows:
                exists = conn.execute(
                    "SELECT 1 FROM notifications WHERE kind='breach_stale' AND ref_id=?",
                    (r["id"],),
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO notifications(kind, ref_type, ref_id, message, created_at) "
                        "VALUES('breach_stale','command',?,?,?)",
                        (r["id"], f"越限指令 #{r['id']} 已等待批准超过 30 分钟", now_iso()),
                    )
            conn.commit()
        finally:
            conn.close()
