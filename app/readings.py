"""测点读数：实时采集、断点补录、趋势查询、小时汇总。"""
from __future__ import annotations

from datetime import datetime, timedelta

from .db import now_iso, query, query_one, tx


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def add_reading(point_id: int, ts: str, value: float):
    """模拟适配器实时上报。重复点幂等忽略，不覆盖已有读数。"""
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO readings(point_id, ts, value, backfilled) VALUES(?,?,?,0)",
            (point_id, ts, float(value)),
        )


def backfill_reading(point_id: int, ts: str, value: float, user) -> dict:
    """断点补录：允许写入历史空缺，并将该测点未处置的断点事件置为已处置。"""
    point = query_one("SELECT * FROM points WHERE id=?", (point_id,))
    if not point:
        raise ValueError("测点不存在")
    with tx() as conn:
        existed = conn.execute(
            "SELECT 1 FROM readings WHERE point_id=? AND ts=?", (point_id, ts)
        ).fetchone()
        conn.execute(
            "INSERT INTO readings(point_id, ts, value, backfilled) VALUES(?,?,?,1) "
            "ON CONFLICT(point_id, ts) DO UPDATE SET value=excluded.value, backfilled=1",
            (point_id, ts, float(value)),
        )
        conn.execute(
            "UPDATE events SET status='handled', handled_by=?, handled_at=?, "
            "resolution=COALESCE(resolution,'') || ? WHERE type='sensor_gap' AND status='open' "
            "AND json_extract(payload_json,'$.point_id')=?",
            (user["id"], now_iso(),
             f"[{now_iso()}] 断点补录 {ts} = {value}；", point_id),
        )
    return {"point_id": point_id, "ts": ts, "value": float(value),
            "overwrote": bool(existed)}


def latest_readings(station_id: int):
    rows = query(
        """
        SELECT p.*, r.ts AS latest_ts, r.value AS latest_value, r.backfilled
        FROM points p LEFT JOIN (
            SELECT r1.* FROM readings r1
            JOIN (SELECT point_id, MAX(ts) m FROM readings GROUP BY point_id) x
              ON x.point_id=r1.point_id AND x.m=r1.ts
        ) r ON r.point_id=p.id
        WHERE p.station_id=?
        ORDER BY p.building_id IS NOT NULL, p.code
        """,
        (station_id,),
    )
    return rows


def series(point_ids, start: str | None, end: str | None, max_points: int = 600):
    if not point_ids:
        return {}
    conn = None
    from .db import get_conn
    conn = get_conn()
    try:
        q = ("SELECT point_id, ts, value, backfilled FROM readings "
             "WHERE point_id IN (%s)" % ",".join("?" * len(point_ids)))
        args = list(point_ids)
        if start:
            q += " AND ts>=?"
            args.append(start)
        if end:
            q += " AND ts<=?"
            args.append(end)
        q += " ORDER BY point_id, ts"
        rows = conn.execute(q, args).fetchall()
    finally:
        conn.close()
    out: dict[int, list] = {pid: [] for pid in point_ids}
    for r in rows:
        out[r["point_id"]].append({"ts": r["ts"], "v": r["value"],
                                   "b": r["backfilled"]})
    # 抽样限流，避免趋势返回过密
    for pid, vals in out.items():
        if len(vals) > max_points:
            step = len(vals) / max_points
            picked = [vals[int(i * step)] for i in range(max_points)]
            if vals[-1] not in picked:
                picked[-1] = vals[-1]
            out[pid] = picked
    return out


def hourly_rollup(conn=None):
    """把已成历史的整点小时数据汇总进 hourly_summary。

    读数与边界都带相同的本地时区偏移，字符串比较即可正确排序。
    """
    from datetime import datetime
    own = conn is None
    if own:
        from .db import get_conn
        conn = get_conn()
    try:
        now = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
        cutoff = now.isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT OR IGNORE INTO hourly_summary(point_id, hour, avg_value, min_value,
                max_value, sample_count, gap)
            SELECT point_id, substr(ts,1,13)||':00'||substr(ts,16),
                   AVG(value), MIN(value), MAX(value), COUNT(*), 0
            FROM readings
            WHERE ts < ?
            GROUP BY point_id, substr(ts,1,13), substr(ts,16)
            """,
            (cutoff,),
        )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()
