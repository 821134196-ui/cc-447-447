"""班次交接与不可覆盖运行快照。"""
from __future__ import annotations

import json
import os
import uuid

from .db import ATTACH_DIR, dumps, now_iso, query, query_one, tx

SHIFT_KINDS = {"day": "白班", "night": "夜班"}


def open_shift(station_id: int, kind: str, user) -> dict:
    if kind not in SHIFT_KINDS:
        raise ValueError("班次类型必须是 day/night")
    with tx() as conn:
        active = conn.execute(
            "SELECT * FROM shifts WHERE station_id=? AND ended_at IS NULL",
            (station_id,),
        ).fetchone()
        if active:
            raise ValueError(f"当前已有未交接班次 #{active['seq']}（{SHIFT_KINDS[active['kind']]}）")
        row = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next FROM shifts WHERE station_id=?",
            (station_id,),
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO shifts(station_id, seq, kind, operator_id, started_at) "
            "VALUES(?,?,?,?,?) RETURNING *",
            (station_id, row["next"], kind, user["id"], now_iso()),
        ).fetchone()
        return dict(cur)


def active_shift(station_id: int):
    return query_one(
        "SELECT s.*, u.display_name AS operator_name FROM shifts s "
        "JOIN users u ON u.id=s.operator_id WHERE s.station_id=? AND ended_at IS NULL",
        (station_id,),
    )


def take_snapshot(station_id: int, user, note: str = "", window_minutes: int = 60,
                  at: str | None = None, shift_id: int | None = None) -> dict:
    """冻结当前窗口内全部读数副本并计算测点汇总。快照一经写入不可修改。

    at/shift_id 供种子数据为历史班次补拍快照使用。
    """
    if shift_id is None:
        shift = active_shift(station_id)
        if not shift:
            raise ValueError("没有进行中的班次，无法形成快照")
        shift_id = shift["id"]
    points = query("SELECT * FROM points WHERE station_id=?", (station_id,))
    end = at or now_iso()
    from datetime import datetime, timedelta
    start = (datetime.fromisoformat(end) - timedelta(minutes=window_minutes)).isoformat(
        timespec="seconds")
    with tx() as conn:
        snap = conn.execute(
            "INSERT INTO snapshots(shift_id, taken_at, window_start, window_end, note) "
            "VALUES(?,?,?,?,?) RETURNING *",
            (shift_id, end, start, end, note),
        ).fetchone()
        snap_id = snap["id"]
        for p in points:
            rows = conn.execute(
                "SELECT ts, value, backfilled FROM readings WHERE point_id=? AND ts BETWEEN ? AND ?",
                (p["id"], start, end),
            ).fetchall()
            conn.executemany(
                "INSERT INTO snapshot_readings(snapshot_id, point_id, ts, value, backfilled) "
                "VALUES(?,?,?,?,?)",
                [(snap_id, p["id"], r["ts"], r["value"], r["backfilled"]) for r in rows],
            )
            vals = [r["value"] for r in rows]
            # 窗口内超过 10 分钟无读数计为断点数
            gap = _gap_count(rows)
            conn.execute(
                "INSERT INTO snapshot_points(snapshot_id, point_id, avg_value, min_value, "
                "max_value, latest_value, latest_ts, sample_count, gap_count) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (snap_id, p["id"],
                 round(sum(vals) / len(vals), 2) if vals else None,
                 min(vals) if vals else None, max(vals) if vals else None,
                 vals[-1] if vals else None,
                 rows[-1]["ts"] if rows else None, len(vals), gap),
            )
        return dict(snap)


def _gap_count(rows, max_gap_seconds: int = 600) -> int:
    if len(rows) < 2:
        return 0
    from datetime import datetime
    n = 0
    for a, b in zip(rows, rows[1:]):
        dt = (datetime.fromisoformat(b["ts"]) - datetime.fromisoformat(a["ts"])).total_seconds()
        if dt > max_gap_seconds:
            n += int(dt // max_gap_seconds)
    return n


def close_shift(station_id: int, user, note: str, auto_snapshot: bool = True) -> dict:
    if auto_snapshot:
        # 交接前自动形成末次不可覆盖快照
        take_snapshot(station_id, user, note="交接自动快照")
    with tx() as conn:
        shift = conn.execute(
            "SELECT * FROM shifts WHERE station_id=? AND ended_at IS NULL",
            (station_id,),
        ).fetchone()
        if not shift:
            raise ValueError("没有进行中的班次")
        conn.execute(
            "UPDATE shifts SET ended_at=?, handover_note=? WHERE id=?",
            (now_iso(), note, shift["id"]),
        )
        return dict(conn.execute("SELECT * FROM shifts WHERE id=?", (shift["id"],)).fetchone())


def list_shifts(station_id: int):
    return query(
        "SELECT s.*, u.display_name AS operator_name FROM shifts s "
        "JOIN users u ON u.id=s.operator_id WHERE s.station_id=? ORDER BY s.seq DESC",
        (station_id,),
    )


def snapshot_detail(snapshot_id: int):
    snap = query_one("SELECT * FROM snapshots WHERE id=?", (snapshot_id,))
    if not snap:
        return None
    pts = query(
        "SELECT sp.*, p.code, p.label, p.unit, p.kind FROM snapshot_points sp "
        "JOIN points p ON p.id=sp.point_id WHERE sp.snapshot_id=? ORDER BY p.code",
        (snapshot_id,),
    )
    return {"snapshot": snap, "points": pts}


def add_attachment(shift_id: int, filename: str, data: bytes, user) -> dict:
    stored = f"{shift_id}_{uuid.uuid4().hex[:8]}_{os.path.basename(filename)}"
    path = os.path.join(ATTACH_DIR, stored)
    with open(path, "wb") as f:
        f.write(data)
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO shift_attachments(shift_id, filename, stored_name, size, "
            "uploaded_by, uploaded_at) VALUES(?,?,?,?,?,?) RETURNING *",
            (shift_id, filename, stored, len(data), user["id"], now_iso()),
        ).fetchone()
        return dict(cur)


def list_attachments(shift_id: int):
    return query(
        "SELECT a.*, u.display_name AS uploader_name FROM shift_attachments a "
        "JOIN users u ON u.id=a.uploaded_by WHERE shift_id=? ORDER BY uploaded_at",
        (shift_id,),
    )


def attachment_path(stored_name: str):
    # 防目录穿越
    safe = os.path.basename(stored_name)
    p = os.path.join(ATTACH_DIR, safe)
    return p if os.path.exists(p) else None
