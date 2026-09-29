"""居民报修：受理、重复/集中归并、处置、回访。

- 同住户 24h 内未结案的同类报修 → 归并为重复报修（repeat_repair 事件）
- 同楼栋 60 分钟内 ≥3 件冷暖报修 → 标记集中不热（mass_cold 事件）
- 回访不满意 → 重开工单并产生 callback_unsatisfied 事件
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .auth import can_see_resident_detail, mask_resident
from .db import dumps, now_iso, query, query_one, tx

MASS_WINDOW_MIN = 60
MASS_THRESHOLD = 3
REPEAT_HOURS = 24


def create_ticket(building_id: int, room: str, channel: str, category: str,
                  description: str, resident_id=None):
    with tx() as conn:
        resident = None
        if resident_id:
            resident = conn.execute(
                "SELECT * FROM residents WHERE id=? AND building_id=?",
                (resident_id, building_id),
            ).fetchone()
        if not resident and room:
            resident = conn.execute(
                "SELECT * FROM residents WHERE building_id=? AND room=?",
                (building_id, room),
            ).fetchone()
        cur = conn.execute(
            "INSERT INTO tickets(building_id, resident_id, room, channel, category, "
            "description, created_at) VALUES(?,?,?,?,?,?,?) RETURNING *",
            (building_id, resident["id"] if resident else None,
             room or (resident["room"] if resident else None),
             channel, category, description, now_iso()),
        ).fetchone()
        tid = cur["id"]

        # 重复报修：同楼栋同房号、同类、24h 内仍未结案
        repeat_of = conn.execute(
            "SELECT * FROM tickets WHERE building_id=? AND IFNULL(room,'')=IFNULL(?,'') "
            "AND category=? AND id!=? AND state IN ('open','processing','reopened') "
            "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (building_id, room or "", category, tid,
             (datetime.now().astimezone() - timedelta(hours=REPEAT_HOURS)).isoformat(
                 timespec="seconds")),
        ).fetchone()
        if repeat_of:
            conn.execute(
                "UPDATE tickets SET state='merged', master_id=? WHERE id=?",
                (repeat_of["id"], tid),
            )
            conn.execute(
                "INSERT INTO events(type, severity, station_id, building_id, "
                "detected_at, payload_json) VALUES('repeat_repair','info',"
                "(SELECT station_id FROM buildings WHERE id=?),?,?,?)",
                (building_id, building_id, now_iso(), dumps(
                    {"ticket_id": tid, "master_id": repeat_of["id"],
                     "room": room or ""})),
            )

        # 集中不热/热：窗口内同楼栋冷暖报修数
        since = (datetime.now().astimezone()
                 - timedelta(minutes=MASS_WINDOW_MIN)).isoformat(timespec="seconds")
        cnt = conn.execute(
            "SELECT COUNT(*) c FROM tickets WHERE building_id=? AND category IN ('cold','hot') "
            "AND created_at>=?",
            (building_id, since),
        ).fetchone()["c"]
        already = conn.execute(
            "SELECT 1 FROM events WHERE type='mass_cold' AND status='open' "
            "AND building_id=? AND detected_at>=?",
            (building_id, since),
        ).fetchone()
        if cnt >= MASS_THRESHOLD and not already:
            conn.execute(
                "INSERT INTO events(type, severity, station_id, building_id, "
                "detected_at, payload_json) VALUES('mass_cold','critical',"
                "(SELECT station_id FROM buildings WHERE id=?),?,?,?)",
                (building_id, building_id, now_iso(), dumps(
                    {"window_min": MASS_WINDOW_MIN, "count": cnt})),
            )
        final = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        return dict(final)


def handle_ticket(ticket_id: int, user, note: str):
    with tx() as conn:
        t = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not t:
            raise ValueError("报修单不存在")
        if t["state"] not in ("open", "reopened"):
            raise ValueError("仅待处理/重开状态的报修单可登记处置")
        conn.execute(
            "UPDATE tickets SET state='processing', handled_by=?, handled_at=?, "
            "handle_note=? WHERE id=?",
            (user["id"], now_iso(), note, ticket_id),
        )
    return get_ticket(ticket_id, user)


def resolve_ticket(ticket_id: int, user, note: str):
    with tx() as conn:
        t = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not t:
            raise ValueError("报修单不存在")
        conn.execute(
            "UPDATE tickets SET state='resolved', handle_note=handle_note || ? WHERE id=?",
            (f"[{now_iso()}] {note}；", ticket_id),
        )
    return get_ticket(ticket_id, user)


def callback(ticket_id: int, user, satisfied: bool, note: str):
    """客服回访；不满意则重开工单并生成 callback_unsatisfied 事件。"""
    with tx() as conn:
        t = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not t:
            raise ValueError("报修单不存在")
        conn.execute(
            "UPDATE tickets SET callback_state=?, callback_by=?, callback_at=?, "
            "callback_note=? WHERE id=?",
            ("satisfied" if satisfied else "dissatisfied",
             user["id"], now_iso(), note, ticket_id),
        )
        if not satisfied:
            conn.execute(
                "UPDATE tickets SET state='reopened' WHERE id=?", (ticket_id,))
            conn.execute(
                "INSERT INTO events(type, severity, station_id, building_id, "
                "detected_at, payload_json) VALUES('callback_unsatisfied','warning',"
                "(SELECT station_id FROM buildings WHERE id=?),?,?,?)",
                (t["building_id"], t["building_id"], now_iso(), dumps(
                    {"ticket_id": ticket_id, "note": note})),
            )
    return get_ticket(ticket_id, user)


def get_ticket(ticket_id: int, user=None):
    row = query_one(
        """
        SELECT t.*, b.name AS building_name, r.name AS resident_name,
               r.phone AS resident_phone,
               h.display_name AS handler_name, cb.display_name AS callback_name
        FROM tickets t
        JOIN buildings b ON b.id=t.building_id
        LEFT JOIN residents r ON r.id=t.resident_id
        LEFT JOIN users h ON h.id=t.handled_by
        LEFT JOIN users cb ON cb.id=t.callback_by
        WHERE t.id=?
        """,
        (ticket_id,),
    )
    return mask_resident(row, user) if row else None


def list_tickets(state=None, building_id=None, user=None):
    q = (
        """
        SELECT t.*, b.name AS building_name, r.name AS resident_name,
               r.phone AS resident_phone
        FROM tickets t JOIN buildings b ON b.id=t.building_id
        LEFT JOIN residents r ON r.id=t.resident_id WHERE 1=1
        """)
    args = []
    if state:
        q += " AND t.state=?"
        args.append(state)
    if building_id:
        q += " AND t.building_id=?"
        args.append(building_id)
    q += " ORDER BY t.created_at DESC"
    rows = query(q, args)
    return [mask_resident(r, user) for r in rows]


def density_by_building(hours: int = 24):
    """近 N 小时各楼栋报修密度（供表格微型条形）。"""
    since = (datetime.now().astimezone()
             - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = query(
        """
        SELECT b.id AS building_id, b.name AS building_name, st.name AS station_name,
               SUM(CASE WHEN t.category='cold' THEN 1 ELSE 0 END) AS cold_n,
               SUM(CASE WHEN t.category='hot' THEN 1 ELSE 0 END) AS hot_n,
               SUM(CASE WHEN t.category NOT IN ('cold','hot') THEN 1 ELSE 0 END) AS other_n,
               COUNT(t.id) AS total_n,
               SUM(CASE WHEN t.state IN ('open','reopened') THEN 1 ELSE 0 END) AS open_n
        FROM buildings b JOIN stations st ON st.id=b.station_id
        LEFT JOIN tickets t ON t.building_id=b.id AND t.created_at>=?
        GROUP BY b.id ORDER BY total_n DESC
        """,
        (since,),
    )
    peak = max([r["total_n"] for r in rows], default=1) or 1
    for r in rows:
        r["ratio"] = round(r["total_n"] / peak, 3)
    return rows
