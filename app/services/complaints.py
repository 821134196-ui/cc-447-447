"""居民报修：重复报修归并、集中不热识别、处置与不满意回访。

- 重复报修：同住户同楼栋 24h 内再次报修 → 新单 status='merged' 并入首单；
  同时产生/维持 repeat_repair 事件（七类事件之一，单独处置）。
- 集中不热：同一站点 60min 窗内出现 ≥3 个不同楼栋来源的独立不热报修 →
  产生 mass_cold 事件，不并入普通单。
- 回访不满意：产生 callback_unsatisfied 事件，单独处置（不与重复报修混淆）。
"""
import json
from datetime import timedelta

from .. import config
from ..db import row_to_dict
from . import timeutil as t


KIND_LABELS = {
    "cold": "不热", "hot": "过热", "noise": "异响", "leak": "漏水", "other": "其他"
}


def create_complaint(conn, station_id, building_id, kind, content,
                     resident_id=None, source="phone", created_at=None):
    """登记报修，返回 (报修行, 归并目标或None, 是否触发集中不热)。"""
    created_at = created_at or t.iso_now()
    window_start = t.fmt(t.parse(created_at) - timedelta(hours=config.REPEAT_WINDOW_HOURS))
    merged_into = None

    # 1) 重复报修归并：同住户 + 同楼栋 + 时间窗 + 未被归并的前序单
    if resident_id is not None:
        prev = conn.execute(
            """SELECT * FROM complaints
               WHERE resident_id=? AND building_id=? AND status<>'merged'
                 AND created_at>=? AND created_at<=?
               ORDER BY created_at DESC LIMIT 1""",
            (resident_id, building_id, window_start, created_at)).fetchone()
        if prev:
            cur = conn.execute(
                """INSERT INTO complaints(station_id, building_id, resident_id, kind,
                       content, status, merged_into, source, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (station_id, building_id, resident_id, kind,
                 f"【归并】{content}", "merged", prev["id"], source, created_at))
            conn.execute(
                "UPDATE complaints SET content=content||? WHERE id=?",
                (f" ｜追访({created_at[5:16]}):{content}", prev["id"]))
            merged = row_to_dict(conn.execute(
                "SELECT * FROM complaints WHERE id=?", (cur.lastrowid,)).fetchone())
            _repeat_event(conn, station_id, building_id, prev["id"], created_at)
            conn.commit()
            return merged, row_to_dict(prev), False

    cur = conn.execute(
        """INSERT INTO complaints(station_id, building_id, resident_id, kind,
               content, status, source, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (station_id, building_id, resident_id, kind, content, "open",
         source, created_at))
    row = row_to_dict(conn.execute("SELECT * FROM complaints WHERE id=?",
                                   (cur.lastrowid,)).fetchone())

    # 2) 集中不热：时间窗内跨楼栋独立不热报修计数
    mass = False
    if kind == "cold":
        w0 = t.fmt(t.parse(created_at) - timedelta(minutes=config.MASS_COMPLAINT_WINDOW_MIN))
        distinct = conn.execute(
            """SELECT COUNT(DISTINCT building_id) AS n FROM complaints
               WHERE station_id=? AND kind='cold' AND status<>'merged'
                 AND created_at>=? AND created_at<=?""",
            (station_id, w0, created_at)).fetchone()["n"]
        if distinct >= config.MASS_COMPLAINT_COUNT:
            _mass_event(conn, station_id, w0, created_at, distinct)
            mass = True

    conn.commit()
    return row, None, mass


def _repeat_event(conn, station_id, building_id, root_id, at):
    """重复报修事件：同一根单的多次追访只维护一条未关闭事件。"""
    row = conn.execute(
        """SELECT * FROM events WHERE kind='repeat_repair' AND status='open'
           AND related_id=?""", (root_id,)).fetchone()
    detail = {"root_complaint": root_id, "building_id": building_id}
    if row:
        d = json.loads(row["detail_json"])
        d["count"] = d.get("count", 1) + 1
        conn.execute("UPDATE events SET detail_json=? WHERE id=?",
                     (json.dumps(d, ensure_ascii=False), row["id"]))
    else:
        detail["count"] = 2
        conn.execute(
            """INSERT INTO events(kind, severity, status, station_id, building_id,
                   detail_json, related_id, started_at)
               VALUES('repeat_repair','warning','open',?,?,?,?,?)""",
            (station_id, building_id, json.dumps(detail, ensure_ascii=False),
             root_id, at))
        conn.execute(
            "INSERT INTO notifications(level,title,body,kind,created_at) "
            "VALUES('warning','重复报修',?,'repeat_repair',?)",
            (f"楼栋{building_id} 报修 #{root_id} 出现重复报修", at))


def _mass_event(conn, station_id, w0, w1, distinct):
    exists = conn.execute(
        "SELECT id FROM events WHERE kind='mass_cold' AND status='open' AND station_id=?",
        (station_id,)).fetchone()
    if exists:
        return
    detail = {"window": [w0, w1], "distinct_buildings": distinct}
    conn.execute(
        """INSERT INTO events(kind, severity, status, station_id, building_id,
               detail_json, started_at)
           VALUES('mass_cold','critical','open',?,NULL,?,?)""",
        (station_id, json.dumps(detail, ensure_ascii=False), w1))
    conn.execute(
        "INSERT INTO notifications(level,title,body,kind,created_at) "
        "VALUES('danger','集中不热',?,'mass_cold',?)",
        (f"站点{station_id} {config.MASS_COMPLAINT_WINDOW_MIN}分钟内"
         f"{distinct} 栋楼报不热", w1))


def resolve(conn, complaint_id, user, note="处置完成"):
    row = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
    if not row:
        raise ValueError("报修不存在")
    if row["status"] == "merged":
        raise ValueError("归并单不可单独处置，请处理主单")
    conn.execute(
        "UPDATE complaints SET status='resolved', resolved_at=? WHERE id=?",
        (t.iso_now(), complaint_id))
    conn.commit()
    return row_to_dict(conn.execute("SELECT * FROM complaints WHERE id=?",
                                    (complaint_id,)).fetchone())


def add_callback(conn, complaint_id, result, note, user):
    """回访：satisfied / unsatisfied；不满意单独开事件。"""
    cmp_ = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
    if not cmp_:
        raise ValueError("报修不存在")
    cur = conn.execute(
        """INSERT INTO callbacks(complaint_id, result, note, operator_id, created_at)
           VALUES(?,?,?,?,?)""",
        (complaint_id, result, note, user["id"], t.iso_now()))
    if result == "unsatisfied":
        exists = conn.execute(
            """SELECT id FROM events WHERE kind='callback_unsatisfied' AND status='open'
               AND related_id=?""", (complaint_id,)).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO events(kind, severity, status, station_id, building_id,
                       detail_json, related_id, started_at)
                   VALUES('callback_unsatisfied','warning','open',?,?,?,?,?)""",
                (cmp_["station_id"], cmp_["building_id"],
                 json.dumps({"complaint": complaint_id, "note": note}, ensure_ascii=False),
                 complaint_id, t.iso_now()))
            conn.execute(
                "INSERT INTO notifications(level,title,body,kind,created_at) "
                "VALUES('warning','回访不满意',?,'callback_unsatisfied',?)",
                (f"报修 #{complaint_id} 回访不满意，需再次处置", t.iso_now()))
    conn.commit()
    return row_to_dict(conn.execute("SELECT * FROM callbacks WHERE id=?",
                                    (cur.lastrowid,)).fetchone())


def list_complaints(conn, station_id=None, include_merged=True, limit=300):
    sql = ("""SELECT c.*, b.name AS building_name,
                     r.name AS resident_name, r.phone AS resident_phone, r.room
              FROM complaints c
              JOIN buildings b ON b.id=c.building_id
              LEFT JOIN residents r ON r.id=c.resident_id
              WHERE 1=1""")
    args = []
    if station_id:
        sql += " AND c.station_id=?"
        args.append(station_id)
    if not include_merged:
        sql += " AND c.status<>'merged'"
    sql += " ORDER BY c.created_at DESC LIMIT ?"
    args.append(limit)
    return [row_to_dict(r) for r in conn.execute(sql, args).fetchall()]


def density_by_building(conn, station_id, hours=24):
    """各楼栋近 N 小时报修密度（表格微型条形用）。"""
    w0 = t.hours_ago(hours)
    rows = conn.execute(
        """SELECT b.id AS building_id, b.name AS building_name,
                  COUNT(c.id) AS total,
                  SUM(CASE WHEN c.kind='cold' THEN 1 ELSE 0 END) AS cold,
                  SUM(CASE WHEN c.status='open' THEN 1 ELSE 0 END) AS open_n
           FROM buildings b LEFT JOIN complaints c
             ON c.building_id=b.id AND c.created_at>=? AND c.status<>'merged'
           WHERE b.station_id=?
           GROUP BY b.id ORDER BY total DESC""", (w0, station_id)).fetchall()
    out = [dict(r) for r in rows]
    mx = max((r["total"] for r in out), default=1) or 1
    for r in out:
        r["ratio"] = round(r["total"] / mx, 3)
    return out


def callback_stats(conn, station_id, days=7):
    since = t.fmt(t.now().replace(hour=0, minute=0, second=0) - timedelta(days=days - 1))
    rows = conn.execute(
        """SELECT date(cb.created_at) AS d,
                  SUM(CASE WHEN cb.result='satisfied' THEN 1 ELSE 0 END) AS ok,
                  SUM(CASE WHEN cb.result='unsatisfied' THEN 1 ELSE 0 END) AS bad,
                  COUNT(*) AS total
           FROM callbacks cb JOIN complaints c ON c.id=cb.complaint_id
           WHERE c.station_id=? AND cb.created_at>=?
           GROUP BY d ORDER BY d""", (station_id, since)).fetchall()
    days_rows = [dict(r) for r in rows]
    total = sum(r["total"] for r in days_rows)
    bad = sum(r["bad"] for r in days_rows)
    rate = round((total - bad) * 100 / total, 1) if total else 100.0
    return {"by_day": days_rows, "total": total, "unsatisfied": bad,
            "satisfaction": rate}
