"""运行事件检测与分类处置。

七类事件分开建单、分角色处置：
  sensor_gap          传感器断点   —— 运维（断点补录后自动关闭）
  temp_diff_abnormal  温差异常     —— 值班员
  pressure_drop       压力骤降     —— 值班员
  pump_switch         水泵切换     —— 运维
  mass_cold           集中不热     —— 值班员
  repeat_repair       重复报修     —— 客服
  callback_unsatisfied 回访不满意  —— 客服
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .db import dumps, now_iso, query, query_one, tx

# 判定参数
GAP_MINUTES = 4            # 超过此时长无读数判为断点
DIFF_LOW, DIFF_HIGH = 5.0, 20.0
PRESSURE_DROP = 0.08       # 5 分钟内压降阈值 MPa
PUMP_LOBACK_MIN = 15       # 同类压降事件去重窗口

HANDLER_ROLES = {
    "sensor_gap": ("ops",),
    "temp_diff_abnormal": ("dispatcher", "manager"),
    "pressure_drop": ("dispatcher", "manager"),
    "pump_switch": ("ops",),
    "mass_cold": ("dispatcher", "manager"),
    "repeat_repair": ("service",),
    "callback_unsatisfied": ("service", "dispatcher"),
}

EVENT_NAMES = {
    "sensor_gap": "传感器断点",
    "temp_diff_abnormal": "温差异常",
    "pressure_drop": "压力骤降",
    "pump_switch": "水泵切换",
    "mass_cold": "集中不热",
    "repeat_repair": "重复报修",
    "callback_unsatisfied": "回访不满意",
}


def _open_exists(conn, etype, key_sql, key_args):
    return conn.execute(
        f"SELECT 1 FROM events WHERE type=? AND status='open' AND {key_sql}",
        (etype, *key_args),
    ).fetchone()


def detect(notify=True):
    """由后台任务周期调用，扫描最近读数生成事件。返回新建事件 id 列表。"""
    created = []
    now = datetime.now().astimezone()
    with tx() as conn:
        points = conn.execute("SELECT * FROM points").fetchall()
        pmap = {p["id"]: dict(p) for p in points}

        def latest(pid):
            r = conn.execute(
                "SELECT ts, value FROM readings WHERE point_id=? ORDER BY ts DESC LIMIT 1",
                (pid,),
            ).fetchone()
            return (datetime.fromisoformat(r["ts"]), r["value"]) if r else (None, None)

        # 1) 传感器断点
        for p in points:
            ts, val = latest(p["id"])
            age = (now - ts).total_seconds() / 60 if ts else 9999
            if age > GAP_MINUTES and not _open_exists(
                conn, "sensor_gap",
                "json_extract(payload_json,'$.point_id')=?", (p["id"],),
            ):
                cur = conn.execute(
                    "INSERT INTO events(type, severity, station_id, building_id, "
                    "detected_at, payload_json) VALUES('sensor_gap','warning',?,?,?,?)",
                    (p["station_id"], p["building_id"], now_iso(),
                     dumps({"point_id": p["id"], "point_code": p["code"],
                            "last_ts": ts.isoformat() if ts else None,
                            "age_min": round(age, 1)})),
                )
                created.append(cur.lastrowid)
            elif age <= GAP_MINUTES:
                # 数据上报恢复：自动关闭该测点的开放断点（补录/现场处理仍可手动关闭）
                conn.execute(
                    "UPDATE events SET status='handled', handled_at=?, resolution="
                    "COALESCE(resolution,'') || ? WHERE type='sensor_gap' AND status='open' "
                    "AND json_extract(payload_json,'$.point_id')=?",
                    (now_iso(), f"[{now_iso()}] 数据上报自动恢复；", p["id"]))

        # 2) 温差异常（按楼栋配对供/回水温度）
        by_building: dict[int, dict] = {}
        for p in points:
            if p["building_id"] and p["kind"] in ("supply_temp", "return_temp"):
                by_building.setdefault(p["building_id"], {})[p["kind"]] = p
        for bid, pair in by_building.items():
            if "supply_temp" not in pair or "return_temp" not in pair:
                continue
            ts_s, v_s = latest(pair["supply_temp"]["id"])
            ts_r, v_r = latest(pair["return_temp"]["id"])
            if not ts_s or not ts_r:
                continue
            if (now - ts_s).total_seconds() > GAP_MINUTES * 60:
                continue
            diff = v_s - v_r
            if not (DIFF_LOW <= diff <= DIFF_HIGH) and not _open_exists(
                conn, "temp_diff_abnormal", "building_id=?", (bid,)
            ):
                cur = conn.execute(
                    "INSERT INTO events(type, severity, station_id, building_id, "
                    "detected_at, payload_json) VALUES('temp_diff_abnormal',?,?,?,?,?)",
                    ("critical" if diff < DIFF_LOW else "warning",
                     pair["supply_temp"]["station_id"], bid, now_iso(),
                     dumps({"supply": v_s, "return": v_r, "diff": round(diff, 2)})),
                )
                created.append(cur.lastrowid)
            elif DIFF_LOW <= diff <= DIFF_HIGH:
                conn.execute(
                    "UPDATE events SET status='handled', handled_at=?, resolution="
                    "COALESCE(resolution,'') || ? WHERE type='temp_diff_abnormal' "
                    "AND status='open' AND building_id=?",
                    (now_iso(), f"[{now_iso()}] 温差恢复 {diff:.1f}℃，自动关闭；", bid))

        # 3) 压力骤降（站点供水压力 5 分钟滑窗）
        for p in points:
            if p["kind"] != "supply_pressure":
                continue
            r_now = conn.execute(
                "SELECT ts, value FROM readings WHERE point_id=? ORDER BY ts DESC LIMIT 1",
                (p["id"],),
            ).fetchone()
            if not r_now:
                continue
            t_now = datetime.fromisoformat(r_now["ts"])
            if (now - t_now).total_seconds() > GAP_MINUTES * 60:
                continue
            r_old = conn.execute(
                "SELECT ts, value FROM readings WHERE point_id=? AND ts<=? "
                "ORDER BY ts DESC LIMIT 1",
                (p["id"], (t_now - timedelta(minutes=5)).isoformat(timespec="seconds")),
            ).fetchone()
            if r_old and r_old["value"] - r_now["value"] >= PRESSURE_DROP:
                since = (now - timedelta(minutes=PUMP_LOBACK_MIN)).isoformat(timespec="seconds")
                dup = conn.execute(
                    "SELECT 1 FROM events WHERE type='pressure_drop' AND station_id=? "
                    "AND detected_at>=?", (p["station_id"], since),
                ).fetchone()
                if not dup:
                    cur = conn.execute(
                        "INSERT INTO events(type, severity, station_id, building_id, "
                        "detected_at, payload_json) VALUES('pressure_drop','critical',"
                        "?,NULL,?,?)",
                        (p["station_id"], now_iso(),
                         dumps({"point_code": p["code"],
                                "from": r_old["value"], "to": r_now["value"],
                                "drop": round(r_old["value"] - r_now["value"], 3)})),
                    )
                    created.append(cur.lastrowid)

        # 4) 水泵切换：泵状态值（运行泵编号）变化
        for p in points:
            if p["kind"] != "pump_status":
                continue
            rows = conn.execute(
                "SELECT ts, value FROM readings WHERE point_id=? ORDER BY ts DESC LIMIT 2",
                (p["id"],),
            ).fetchall()
            if len(rows) == 2 and rows[0]["value"] != rows[1]["value"]:
                # 仅最近一条读数是"刚发生"的切换时建单
                t_last = datetime.fromisoformat(rows[0]["ts"])
                if (now - t_last).total_seconds() <= 120:
                    recent = conn.execute(
                        "SELECT 1 FROM events WHERE type='pump_switch' AND station_id=? "
                        "AND detected_at>=?",
                        (p["station_id"],
                         (now - timedelta(minutes=5)).isoformat(timespec="seconds")),
                    ).fetchone()
                    if not recent:
                        cur = conn.execute(
                            "INSERT INTO events(type, severity, station_id, building_id, "
                            "detected_at, payload_json) VALUES('pump_switch','info',"
                            "?,NULL,?,?)",
                            (p["station_id"], now_iso(),
                             dumps({"point_code": p["code"],
                                    "from_pump": rows[1]["value"],
                                    "to_pump": rows[0]["value"]})),
                        )
                        created.append(cur.lastrowid)

        if created and notify:
            for eid in created:
                ev = conn.execute("SELECT type FROM events WHERE id=?", (eid,)).fetchone()
                conn.execute(
                    "INSERT INTO notifications(kind, ref_type, ref_id, message, created_at) "
                    "VALUES('event','event',?,?,?)",
                    (eid, f"新事件：{EVENT_NAMES[ev['type']]}（#{eid}）", now_iso()),
                )
    return created


def handle_event(event_id: int, user, resolution: str):
    with tx() as conn:
        ev = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not ev:
            raise ValueError("事件不存在")
        if ev["status"] == "handled":
            raise ValueError("事件已处置")
        roles = HANDLER_ROLES[ev["type"]]
        if user["role"] not in roles:
            raise PermissionError(
                f"「{EVENT_NAMES[ev['type']]}」须由{'/'.join(roles)}处置")
        conn.execute(
            "UPDATE events SET status='handled', handled_by=?, handled_at=?, "
            "resolution=? WHERE id=?",
            (user["id"], now_iso(), resolution, event_id),
        )
    return get_event(event_id)


def get_event(event_id: int):
    return query_one(
        "SELECT e.*, u.display_name AS handler_name FROM events e "
        "LEFT JOIN users u ON u.id=e.handled_by WHERE e.id=?",
        (event_id,),
    )


def list_events(status=None, etype=None):
    q = ("SELECT e.*, u.display_name AS handler_name, b.name AS building_name, "
         "s.name AS station_name FROM events e "
         "LEFT JOIN users u ON u.id=e.handled_by "
         "LEFT JOIN buildings b ON b.id=e.building_id "
         "LEFT JOIN stations s ON s.id=e.station_id WHERE 1=1")
    args = []
    if status:
        q += " AND e.status=?"
        args.append(status)
    if etype:
        q += " AND e.type=?"
        args.append(etype)
    q += " ORDER BY e.detected_at DESC"
    return query(q, args)
