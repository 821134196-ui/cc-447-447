"""调节指令生命周期：待复核 → 待执行 → 生效观察 → 结束（驳回为旁路终态）。

规则：
- 越限指令复核时必须由供热经理（manager）批准；值班员只能复核非越限指令；
- 运维（ops）可执行但不能批准/复核；
- 生效观察期内，同楼栋的冷/暖报修自动关联到指令，用于结束时判断效果。
"""
import json
from datetime import timedelta

from .. import config
from ..db import row_to_dict
from . import timeutil as t
from . import curves as curve_svc


class CommandError(Exception):
    pass


def submit(conn, station_id, building_id, user_id, note=""):
    """基于该楼栋最新曲线版本创建指令（相对当前生效版本）。"""
    latest = curve_svc.current_version(conn, station_id, building_id)
    if not latest:
        raise CommandError("该楼栋尚无曲线，无法下达指令")
    # 当前生效版本 = 最近一条已结束指令所采用的版本（可能为空）
    last_done = conn.execute(
        """SELECT new_curve_version FROM commands
           WHERE building_id=? AND status='finished'
           ORDER BY id DESC LIMIT 1""", (building_id,)).fetchone()
    old_ver = last_done["new_curve_version"] if last_done else None
    if old_ver == latest["version"]:
        raise CommandError("最新曲线版本已生效，无需重复下达")
    # 该楼栋已有进行中的指令
    busy = conn.execute(
        "SELECT id FROM commands WHERE building_id=? AND status IN "
        "('pending_review','pending_execution','observing')",
        (building_id,)).fetchone()
    if busy:
        raise CommandError("该楼栋存在进行中的指令，请先完结")

    over, details = curve_svc.check_over_limit(latest["points"])
    cur = conn.execute(
        """INSERT INTO commands(station_id, building_id, old_curve_version,
               new_curve_version, status, over_limit, limit_detail,
               submitted_by, submitted_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (station_id, building_id, old_ver, latest["version"],
         "pending_review", 1 if over else 0,
         "；".join(details), user_id, t.iso_now()))
    conn.commit()
    return get(conn, cur.lastrowid)


def review(conn, command_id, user, approve, note=""):
    """复核/批准。越限指令仅 manager 可批准；驳回进入 rejected。"""
    cmd = get(conn, command_id)
    if not cmd or cmd["status"] != "pending_review":
        raise CommandError("指令不在待复核状态")
    if cmd["over_limit"] and user["role"] != "manager":
        raise PermissionError("越限指令必须由供热经理批准")
    if not cmd["over_limit"] and user["role"] not in ("dispatcher", "manager"):
        raise PermissionError("您没有复核权限")
    if not approve:
        conn.execute(
            """UPDATE commands SET status='rejected', reviewed_by=?, reviewed_at=?,
               review_note=? WHERE id=?""",
            (user["id"], t.iso_now(), note or "驳回", command_id))
    else:
        conn.execute(
            """UPDATE commands SET status='pending_execution', reviewed_by=?,
               reviewed_at=?, review_note=? WHERE id=?""",
            (user["id"], t.iso_now(), note or ("越限批准" if cmd["over_limit"] else "复核通过"),
             command_id))
    conn.commit()
    return get(conn, command_id)


def execute(conn, command_id, user):
    """运维执行：待执行 → 生效观察。"""
    if user["role"] not in ("ops", "manager"):
        raise PermissionError("仅运维可执行指令")
    cmd = get(conn, command_id)
    if not cmd or cmd["status"] != "pending_execution":
        raise CommandError("指令不在待执行状态")
    now = t.now()
    observe_until = t.fmt(now + timedelta(minutes=config.OBSERVE_MINUTES))
    conn.execute(
        """UPDATE commands SET status='observing', executed_by=?, executed_at=?,
           observe_until=? WHERE id=?""",
        (user["id"], t.fmt(now), observe_until, command_id))
    conn.commit()
    return get(conn, command_id)


def link_observation_complaints(conn, command_id):
    """把观察窗内同楼栋的冷/暖报修关联到指令（观察期内持续调用）。"""
    cmd = get(conn, command_id)
    if not cmd or cmd["status"] != "observing":
        return 0
    rows = conn.execute(
        """SELECT id FROM complaints
           WHERE building_id=? AND kind IN ('cold','hot') AND status<>'merged'
             AND command_id IS NULL AND created_at>=?""",
        (cmd["building_id"], cmd["executed_at"])).fetchall()
    for r in rows:
        conn.execute("UPDATE complaints SET command_id=? WHERE id=?",
                     (command_id, r["id"]))
    conn.commit()
    return len(rows)


def linked_complaints(conn, command_id):
    return [row_to_dict(r) for r in conn.execute(
        """SELECT c.*, res.name AS resident_name FROM complaints c
           LEFT JOIN residents res ON res.id=c.resident_id
           WHERE c.command_id=? ORDER BY c.created_at""", (command_id,)).fetchall()]


def can_finish(conn, cmd):
    """观察期结束且存在关联报修数据时给出效果判定建议。"""
    if cmd["status"] != "observing":
        return False, "指令不在观察期"
    if t.parse(cmd["observe_until"]) > t.now():
        return False, f"观察期未结束（至 {cmd['observe_until']}）"
    return True, ""


def finish(conn, command_id, user, effect_note=""):
    """结束观察：自动关联观察窗内报修并按结果生成效果结论。"""
    cmd = get(conn, command_id)
    ok, msg = can_finish(conn, cmd)
    if not ok:
        raise CommandError(msg)
    if user["role"] not in ("dispatcher", "manager"):
        raise PermissionError("仅值班员可结束观察并判定效果")
    link_observation_complaints(conn, command_id)
    linked = linked_complaints(conn, command_id)
    colds = [c for c in linked if c["kind"] == "cold"]
    hots = [c for c in linked if c["kind"] == "hot"]
    if not effect_note:
        if len(colds) >= config.MASS_COMPLAINT_COUNT:
            effect_note = f"效果不佳：观察期内仍有 {len(colds)} 单不热报修"
        elif colds:
            effect_note = f"部分改善：仍有 {len(colds)} 单不热、{len(hots)} 单过热"
        elif hots:
            effect_note = f"过热倾向：{len(hots)} 单过热报修"
        else:
            effect_note = "效果良好：观察期内无冷暖报修"
    conn.execute(
        "UPDATE commands SET status='finished', finished_at=?, effect_note=? WHERE id=?",
        (t.iso_now(), effect_note, command_id))
    conn.commit()
    return get(conn, command_id)


def get(conn, command_id):
    return row_to_dict(conn.execute(
        """SELECT cmd.*, bu.display_name AS submitted_name,
                  be.display_name AS executed_name, br.display_name AS reviewed_name
           FROM commands cmd
           JOIN users bu ON bu.id=cmd.submitted_by
           LEFT JOIN users be ON be.id=cmd.executed_by
           LEFT JOIN users br ON br.id=cmd.reviewed_by
           WHERE cmd.id=?""", (command_id,)).fetchone())


def list_(conn, status=None, building_id=None):
    sql = ("SELECT cmd.*, bu.display_name AS submitted_name FROM commands cmd "
           "JOIN users bu ON bu.id=cmd.submitted_by WHERE 1=1")
    args = []
    if status:
        sql += " AND cmd.status=?"
        args.append(status)
    if building_id:
        sql += " AND cmd.building_id=?"
        args.append(building_id)
    sql += " ORDER BY cmd.id DESC"
    return [row_to_dict(r) for r in conn.execute(sql, args).fetchall()]
