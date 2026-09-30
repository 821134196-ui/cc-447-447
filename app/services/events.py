"""运行/服务事件处置：七类事件分开列表、分开处置（关闭原因各不相同）。"""
from ..db import row_to_dict
from . import timeutil as t

EVENT_LABELS = {
    "sensor_break": "传感器断点",
    "temp_diff_abnormal": "温差异常",
    "pressure_drop": "压力骤降",
    "pump_switch": "水泵切换",
    "mass_cold": "集中不热",
    "repeat_repair": "重复报修",
    "callback_unsatisfied": "回访不满意",
}

# 各类事件的标准处置指引（前端分栏展示，不混在一起）
DISPOSE_GUIDE = {
    "sensor_break": "核实通讯与供电；数据恢复后由运维执行断点补录，事件自动关闭",
    "temp_diff_abnormal": "检查换热板换与二网平衡；温差恢复后事件自动关闭",
    "pressure_drop": "排查泄漏与补水；现场消缺后人工关闭并记录",
    "pump_switch": "确认备用泵运行参数，登记切换原因",
    "mass_cold": "升级站点级调度：核查首站出力与平衡阀，统一处置后关闭",
    "repeat_repair": "联系住户二次上门，根因解决并回访满意后关闭",
    "callback_unsatisfied": "重新派单处置，处置完成后再次回访",
}


def list_events(conn, status=None, kind=None, station_id=None):
    sql = "SELECT * FROM events WHERE 1=1"
    args = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if station_id:
        sql += " AND station_id=?"
        args.append(station_id)
    sql += " ORDER BY id DESC LIMIT 300"
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = row_to_dict(r)
        d["kind_label"] = EVENT_LABELS.get(d["kind"], d["kind"])
        d["guide"] = DISPOSE_GUIDE.get(d["kind"], "")
        out.append(d)
    return out


def close_event(conn, event_id, user, note):
    """人工关闭事件。传感器断点建议走补录自动关闭，但也允许人工说明关闭。"""
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise ValueError("事件不存在")
    if row["status"] == "closed":
        raise ValueError("事件已关闭")
    conn.execute(
        "UPDATE events SET status='closed', closed_at=?, close_note=?, closed_by=? "
        "WHERE id=?", (t.iso_now(), note, user["id"], event_id))
    conn.commit()
    return row_to_dict(conn.execute("SELECT * FROM events WHERE id=?",
                                    (event_id,)).fetchone())
