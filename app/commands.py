"""调节指令生命周期。

pending_review 待复核（越限必须站长批准；非越限值班员可复核通过）
  → pending_execution 待执行（运维执行）
  → observing 生效观察（自动关联同楼栋观察窗内冷暖报修）
  → ended 结束（凭关联报修判定效果）
复核驳回 → rejected。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import curves
from .db import dumps, now_iso, query, query_one, tx

OBSERVE_MINUTES = 90


def create_command(building_id: int, points, user, base_version, note=""):
    """值班员提交新目标曲线并生成调节指令。"""
    version, reasons = curves.submit_version(
        building_id, points, user, base_version, note=note, source="command"
    )
    old = curves.active_version(building_id)
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO commands(building_id, new_version_id, old_version_id, status, "
            "breach, breach_reason, submitted_by, submitted_at, observe_minutes) "
            "VALUES(?,?,?,'pending_review',?,?,?,?,?) RETURNING *",
            (building_id, version["id"], old["id"] if old else None,
             1 if reasons else 0, "；".join(reasons),
             user["id"], now_iso(), OBSERVE_MINUTES),
        ).fetchone()
        if reasons:
            conn.execute(
                "INSERT INTO notifications(kind, ref_type, ref_id, message, created_at) "
                "VALUES('breach_approval','command',?,?,?)",
                (cur["id"],
                 f"越限指令 #{cur['id']} 待站长批准：{'；'.join(reasons)}", now_iso()),
            )
        return detail(cur["id"], conn=conn)


def review_command(command_id: int, user, approve: bool, note: str = ""):
    """复核：越限指令只允许站长批准；任何人都可驳回。"""
    with tx() as conn:
        cmd = conn.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if not cmd:
            raise ValueError("指令不存在")
        if cmd["status"] != "pending_review":
            raise ValueError("该指令不在待复核状态")
        if approve and cmd["breach"] and user["role"] != "manager":
            raise PermissionError("越限指令必须由站长批准")
        if approve:
            conn.execute(
                "UPDATE commands SET status='pending_execution', reviewed_by=?, "
                "reviewed_at=?, review_note=? WHERE id=?",
                (user["id"], now_iso(), note, command_id),
            )
        else:
            conn.execute(
                "UPDATE commands SET status='rejected', reviewed_by=?, reviewed_at=?, "
                "review_note=? WHERE id=?",
                (user["id"], now_iso(), note or "复核驳回", command_id),
            )
            conn.execute(
                "UPDATE curve_versions SET status='rejected' WHERE id=?",
                (cmd["new_version_id"],),
            )
        cid = command_id
    return detail(cid)


def execute_command(command_id: int, user):
    """运维执行：旧生效曲线置 superseded，新生效；进入生效观察。"""
    if user["role"] not in ("ops", "manager"):
        raise PermissionError("仅运维人员可执行指令")
    with tx() as conn:
        cmd = conn.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if not cmd:
            raise ValueError("指令不存在")
        if cmd["status"] != "pending_execution":
            raise ValueError("该指令不在待执行状态")
        conn.execute(
            "UPDATE curve_versions SET status='superseded' WHERE building_id=? "
            "AND status='active'",
            (cmd["building_id"],),
        )
        conn.execute(
            "UPDATE curve_versions SET status='active' WHERE id=?",
            (cmd["new_version_id"],),
        )
        conn.execute(
            "UPDATE commands SET status='observing', executed_by=?, executed_at=? WHERE id=?",
            (user["id"], now_iso(), command_id),
        )
        cid = command_id
    return detail(cid)


def link_observation_tickets(conn, cmd):
    """把观察窗内同楼栋冷暖报修关联到指令（幂等）。"""
    start = cmd["executed_at"]
    end_dt = datetime.fromisoformat(start) + timedelta(minutes=cmd["observe_minutes"])
    end = end_dt.isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id FROM tickets WHERE building_id=? AND category IN ('cold','hot') "
        "AND created_at BETWEEN ? AND ?",
        (cmd["building_id"], start, end),
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO command_tickets(command_id, ticket_id, linked_at) "
            "VALUES(?,?,?)",
            (cmd["id"], r["id"], now_iso()),
        )
    return [r["id"] for r in rows]


def end_command(command_id: int, user, judgement: str, note: str = ""):
    """结束观察并依据关联报修判定效果。judgement: effective/ineffective/uncertain。"""
    if judgement not in ("effective", "ineffective", "uncertain"):
        raise ValueError("判定结果无效")
    with tx() as conn:
        cmd = conn.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if not cmd:
            raise ValueError("指令不存在")
        if cmd["status"] != "observing":
            raise ValueError("该指令不在生效观察状态")
        link_observation_tickets(conn, cmd)
        count = conn.execute(
            "SELECT COUNT(*) c FROM command_tickets WHERE command_id=?", (command_id,)
        ).fetchone()["c"]
        conn.execute(
            "UPDATE commands SET status='ended', ended_at=?, effect_judgement=?, "
            "effect=? WHERE id=?",
            (now_iso(), judgement,
             f"观察窗内关联冷暖报修 {count} 件；{note}", command_id),
        )
        cid = command_id
    return detail(cid)


def auto_tick(conn=None):
    """后台调用：观察窗到期的指令自动关联报修（供效果判断，不自动结束）。"""
    own = conn is None
    if own:
        from .db import get_conn
        conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM commands WHERE status='observing'"
        ).fetchall()
        due = []
        for cmd in rows:
            link_observation_tickets(conn, cmd)
            end_dt = datetime.fromisoformat(cmd["executed_at"]) + timedelta(
                minutes=cmd["observe_minutes"])
            if datetime.now().astimezone() >= end_dt:
                due.append(cmd["id"])
        if own:
            conn.commit()
        return due
    finally:
        if own:
            conn.close()


def detail(command_id: int, conn=None):
    own = conn is None
    if own:
        from .db import get_conn
        conn = get_conn()
    try:
        cmd = conn.execute(
            """
            SELECT c.*, b.name AS building_name,
                   su.display_name AS submitter_name,
                   rv.display_name AS reviewer_name,
                   eu.display_name AS executor_name
            FROM commands c
            JOIN buildings b ON b.id=c.building_id
            JOIN users su ON su.id=c.submitted_by
            LEFT JOIN users rv ON rv.id=c.reviewed_by
            LEFT JOIN users eu ON eu.id=c.executed_by
            WHERE c.id=?
            """,
            (command_id,),
        ).fetchone()
        if not cmd:
            return None
        d = dict(cmd)
        nv = conn.execute("SELECT * FROM curve_versions WHERE id=?",
                          (cmd["new_version_id"],)).fetchone()
        ov = (conn.execute("SELECT * FROM curve_versions WHERE id=?",
                           (cmd["old_version_id"],)).fetchone()
              if cmd["old_version_id"] else None)
        d["new_points"] = json.loads(nv["points_json"])
        d["old_points"] = json.loads(ov["points_json"]) if ov else []
        d["tickets"] = [dict(r) for r in conn.execute(
            "SELECT t.id, t.room, t.category, t.state, t.created_at, t.callback_state "
            "FROM command_tickets ct JOIN tickets t ON t.id=ct.ticket_id "
            "WHERE ct.command_id=? ORDER BY t.created_at", (command_id,)).fetchall()]
        return d
    finally:
        if own:
            conn.close()


def list_commands(status: str | None = None, building_id: int | None = None):
    q = ("SELECT c.*, b.name AS building_name, su.display_name AS submitter_name "
         "FROM commands c JOIN buildings b ON b.id=c.building_id "
         "JOIN users su ON su.id=c.submitted_by WHERE 1=1")
    args = []
    if status:
        q += " AND c.status=?"
        args.append(status)
    if building_id:
        q += " AND c.building_id=?"
        args.append(building_id)
    q += " ORDER BY c.submitted_at DESC"
    cmds = query(q, args)
    for c in cmds:
        c["ticket_count"] = query_one(
            "SELECT COUNT(*) c FROM command_tickets WHERE command_id=?", (c["id"],)
        )["c"]
    return cmds
