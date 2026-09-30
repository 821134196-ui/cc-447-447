"""读数采集、异常检测（断点/温差/压力/水泵）与小时汇总。

七类事件中前四类在这里产生：
  sensor_break        传感器断点：相邻读数间隔超过阈值
  temp_diff_abnormal  温差异常：供回水温差过小
  pressure_drop       压力骤降：供水压力短时间下降超阈值
  pump_switch         水泵切换：运行状态发生切换
"""
import json

from .. import config
from ..db import row_to_dict
from . import timeutil as t


def add_reading(conn, station_id, building_id, ts=None, *,
                outdoor_temp, supply_temp, return_temp,
                supply_pressure, return_pressure,
                pump_status, pump_frequency, source="live", quiet=False):
    """写入一条读数并做实时异常检测。返回 (reading_id, 新事件列表)。"""
    ts = ts or t.iso_now()
    cur = conn.execute(
        """INSERT INTO readings(station_id, building_id, ts, outdoor_temp,
               supply_temp, return_temp, supply_pressure, return_pressure,
               pump_status, pump_frequency, source)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (station_id, building_id, ts, outdoor_temp, supply_temp, return_temp,
         supply_pressure, return_pressure, pump_status, pump_frequency, source))
    reading_id = cur.lastrowid

    new_events = []
    if not quiet:
        prev = conn.execute(
            """SELECT * FROM readings WHERE building_id=? AND id<>?
               ORDER BY ts DESC, id DESC LIMIT 1""",
            (building_id, reading_id)).fetchone()

        # 1) 传感器断点（只对实时/模拟数据触发；补录用于关闭断点）
        if source in ("live", "sim") and prev:
            gap_min = (t.parse(ts) - t.parse(prev["ts"])).total_seconds() / 60
            if gap_min > config.SENSOR_GAP_MINUTES:
                ev = _open_event(conn, "sensor_break", "critical", station_id, building_id,
                                 {"from": prev["ts"], "to": ts, "gap_minutes": round(gap_min, 1)},
                                 related_id=reading_id,
                                 started_at=prev["ts"])
                if ev:
                    new_events.append(ev)

        # 2) 温差异常
        diff = supply_temp - return_temp
        if diff < config.DIFF_ABNORMAL:
            ev = _open_event(conn, "temp_diff_abnormal", "warning", station_id, building_id,
                             {"supply": supply_temp, "return": return_temp,
                              "diff": round(diff, 2), "ts": ts},
                             related_id=reading_id, started_at=ts)
            if ev:
                new_events.append(ev)
        else:
            _auto_close(conn, "temp_diff_abnormal", station_id, building_id,
                        f"{ts} 温差恢复至 {diff:.1f}℃")

        # 3) 压力骤降
        if prev:
            drop = prev["supply_pressure"] - supply_pressure
            if drop > config.PRESSURE_DELTA_MAX:
                ev = _open_event(conn, "pressure_drop", "critical", station_id, building_id,
                                 {"before": prev["supply_pressure"], "after": supply_pressure,
                                  "drop": round(drop, 3), "ts": ts},
                                 related_id=reading_id, started_at=ts, duplicate_window=False)
                if ev:
                    new_events.append(ev)

        # 4) 水泵切换
        if prev and prev["pump_status"] != pump_status:
            ev = _open_event(conn, "pump_switch", "info", station_id, building_id,
                             {"from": prev["pump_status"], "to": pump_status,
                              "frequency": pump_frequency, "ts": ts},
                             related_id=reading_id, started_at=ts, duplicate_window=False)
            if ev:
                new_events.append(ev)

        for ev in new_events:
            _notify(conn, ev)
    conn.commit()
    return reading_id, new_events


def backfill_reading(conn, station_id, building_id, **kw):
    """断点补录：插入历史读数（source=backfill），并尝试关闭对应断点事件。

    补录不修改既有快照（快照仍不可覆盖），但会参与后续小时汇总。
    """
    kw["source"] = "backfill"
    rid, events = add_reading(conn, station_id, building_id, quiet=True, **kw)
    ts = kw.get("ts") or t.iso_now()
    # 补录数据落在任一未关闭断点窗口内 → 关闭该断点
    rows = conn.execute(
        """SELECT * FROM events WHERE kind='sensor_break' AND status='open'
           AND station_id=? AND (building_id=? OR building_id IS NULL)""",
        (station_id, building_id)).fetchall()
    closed = []
    for row in rows:
        detail = json.loads(row["detail_json"])
        if detail.get("from", "") <= ts <= detail.get("to", t.iso_now()):
            conn.execute(
                """UPDATE events SET status='closed', closed_at=?,
                   close_note=? WHERE id=?""",
                (ts, f"断点补录：{ts} 数据已补齐（读数 #{rid}）", row["id"]))
            closed.append(row_to_dict(row))
    conn.commit()
    return rid, closed


def _open_event(conn, kind, severity, station_id, building_id, detail,
                related_id=None, started_at=None, duplicate_window=True):
    """产生事件；同类型未关闭事件默认不重复开单（骤降/切换类允许连续开单）。"""
    if duplicate_window:
        exists = conn.execute(
            "SELECT id FROM events WHERE kind=? AND status='open' AND station_id=? "
            "AND COALESCE(building_id,-1)=COALESCE(?,-1)",
            (kind, station_id, building_id)).fetchone()
        if exists:
            return None
    cur = conn.execute(
        """INSERT INTO events(kind, severity, status, station_id, building_id,
               detail_json, related_id, started_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (kind, severity, "open", station_id, building_id,
         json.dumps(detail, ensure_ascii=False), related_id, started_at or t.iso_now()))
    return row_to_dict(conn.execute("SELECT * FROM events WHERE id=?",
                                    (cur.lastrowid,)).fetchone())


def _auto_close(conn, kind, station_id, building_id, note):
    conn.execute(
        """UPDATE events SET status='closed', closed_at=?, close_note=?
           WHERE kind=? AND status='open' AND station_id=?
           AND COALESCE(building_id,-1)=COALESCE(?,-1)""",
        (t.iso_now(), note, kind, station_id, building_id))


def _notify(conn, ev):
    titles = {
        "sensor_break": "传感器断点",
        "temp_diff_abnormal": "温差异常",
        "pressure_drop": "压力骤降",
        "pump_switch": "水泵切换",
    }
    level = {"critical": "danger", "warning": "warning", "info": "info"}[ev["severity"]]
    conn.execute(
        "INSERT INTO notifications(level, title, body, kind, created_at) VALUES(?,?,?,?,?)",
        (level, titles.get(ev["kind"], ev["kind"]),
         f"{titles.get(ev['kind'])} · 站点{ev['station_id']} 楼栋{ev.get('building_id') or '-'}",
         ev["kind"], t.iso_now()))


# ---- 进程内汇总任务 --------------------------------------------------------

def aggregate_hourly(conn):
    """把尚未入汇总表的读数按小时折叠进 aggregates_hourly。返回新增行数。"""
    rows = conn.execute(
        """SELECT station_id, building_id, substr(ts,1,13)||':00:00' AS hour,
                  COUNT(*) AS n, AVG(outdoor_temp) AS ao, AVG(supply_temp) AS asu,
                  AVG(return_temp) AS ar, AVG(supply_pressure) AS ap
           FROM readings
           GROUP BY station_id, building_id, hour""").fetchall()
    inserted = 0
    for r in rows:
        cur = conn.execute(
            """INSERT OR IGNORE INTO aggregates_hourly
               (station_id, building_id, hour, reading_count,
                avg_outdoor, avg_supply, avg_return, avg_pressure)
               VALUES(?,?,?,?,?,?,?,?)""",
            (r["station_id"], r["building_id"], r["hour"], r["n"],
             round(r["ao"], 2), round(r["asu"], 2), round(r["ar"], 2), round(r["ap"], 3)))
        inserted += cur.rowcount
    conn.commit()
    return inserted


def scan_overlimit(conn):
    """越限提醒：检查最新一条读数是否越过安全区间，生成节流通知。"""
    latest = conn.execute(
        """SELECT r.* FROM readings r
           JOIN (SELECT building_id, MAX(ts) AS mts FROM readings GROUP BY building_id) x
             ON r.building_id=x.building_id AND r.ts=x.mts""").fetchall()
    made = 0
    for r in latest:
        problems = []
        if not (config.SUPPLY_MIN <= r["supply_temp"] <= config.SUPPLY_MAX):
            problems.append(f"供水 {r['supply_temp']}℃ 越限")
        if not (config.RETURN_MIN <= r["return_temp"] <= config.RETURN_MAX):
            problems.append(f"回水 {r['return_temp']}℃ 越限")
        d = r["supply_temp"] - r["return_temp"]
        if not (config.DIFF_MIN <= d <= config.DIFF_MAX):
            problems.append(f"温差 {d:.1f}℃ 越限")
        if not problems:
            continue
        body = f"楼栋{r['building_id']}：" + "；".join(problems)
        recent = conn.execute(
            "SELECT id FROM notifications WHERE kind='overlimit' AND body=? "
            "AND created_at > ?", (body, t.hours_ago(1))).fetchone()
        if recent:
            continue
        conn.execute(
            "INSERT INTO notifications(level,title,body,kind,created_at) VALUES(?,?,?,?,?)",
            ("danger", "运行参数越限", body, "overlimit", t.iso_now()))
        made += 1
    conn.commit()
    return made
