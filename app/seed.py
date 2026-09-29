"""预置两天模拟数据：站点、楼栋、住户、测点、读数、班次、快照、
曲线版本、指令全生命周期、报修与回访、七类事件、交接附件。

幂等：已有站点数据时直接跳过。
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta

from . import curves, tickets
from .db import ATTACH_DIR, dumps, get_conn, init_db, now_iso
from .simulator import SimAdapter

DAYS = 2
STEP_MIN = 5

USERS = [
    ("dispatcher1", "王磊", "dispatcher"),
    ("dispatcher2", "李芳", "dispatcher"),
    ("manager1", "赵建国", "manager"),
    ("ops1", "孙强", "ops"),
    ("service1", "周敏", "service"),
]

STATIONS = [
    ("HPL", "和平里换热站", ["1号楼", "2号楼", "3号楼", "4号楼"]),
    ("QH", "清河换热站", ["东区公寓", "西区公寓"]),
]

SURNAMES = list("王李张刘陈杨黄周吴徐孙马朱胡郭何高林罗郑梁谢宋唐许")
GIVEN = list("伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀兰霞平刚桂英华红玉兰建华")

BASE_CURVE = [
    {"outdoor": -20, "supply": 70, "return": 60},
    {"outdoor": -12, "supply": 62, "return": 53},
    {"outdoor": -5, "supply": 55, "return": 46},
    {"outdoor": 0, "supply": 50, "return": 41},
    {"outdoor": 5, "supply": 45, "return": 37},
    {"outdoor": 10, "supply": 42, "return": 35},
]


def seeded() -> bool:
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) c FROM stations").fetchone()["c"] > 0
    finally:
        conn.close()


def seed(force: bool = False):
    init_db()
    if not force and seeded():
        return False
    if force:
        _reset()
    sim = SimAdapter(seed=42)
    now = datetime.now().astimezone().replace(microsecond=0)
    conn = get_conn()
    try:
        uids = {}
        for uname, dname, role in USERS:
            cur = conn.execute(
                "INSERT INTO users(username, display_name, role) VALUES(?,?,?)",
                (uname, dname, role))
            uids[uname] = cur.lastrowid

        station_map = {}
        buildings = []  # (bid, sid, name)
        residents = []
        for code, name, blds in STATIONS:
            sid = conn.execute(
                "INSERT INTO stations(code, name) VALUES(?,?)", (code, name)).lastrowid
            station_map[code] = sid
            # 站级测点
            _point(conn, sid, None, f"{code}_OD", "outdoor_temp", "℃", "室外温度")
            _point(conn, sid, None, f"{code}_PS", "supply_pressure", "MPa", "供水压力")
            _point(conn, sid, None, f"{code}_PR", "return_pressure", "MPa", "回水压力")
            _point(conn, sid, None, f"{code}_PMP", "pump_status", "#", "运行循环泵")
            for i, bname in enumerate(blds, start=1):
                bid = conn.execute(
                    "INSERT INTO buildings(station_id, code, name, area_m2, households) "
                    "VALUES(?,?,?,?,?)",
                    (sid, f"{code}-{i}", bname, 6000 + i * 1300, 72 + i * 12)).lastrowid
                buildings.append((bid, sid, bname))
                _point(conn, sid, bid, f"{code}-{i}_TS", "supply_temp", "℃",
                       f"{bname}供水温度")
                _point(conn, sid, bid, f"{code}-{i}_TR", "return_temp", "℃",
                       f"{bname}回水温度")
                # 模拟住户
                rng = sim.rnd
                for room_no in range(1, 9):
                    rname = rng.choice(SURNAMES) + rng.choice(GIVEN) + (
                        rng.choice(GIVEN) if rng.random() < 0.4 else "")
                    unit = (room_no - 1) // 4 + 1
                    room = f"{unit}单元{300 + room_no}"
                    phone = f"13{rng.randint(0,9)}{rng.randint(10000000,99999999)}"
                    rid = conn.execute(
                        "INSERT INTO residents(building_id, room, name, phone) "
                        "VALUES(?,?,?,?)", (bid, room, rname, phone)).lastrowid
                    residents.append((rid, bid, room))

        pmap = {p["code"]: dict(p) for p in conn.execute("SELECT * FROM points").fetchall()}

        # ---- 曲线版本与指令 ----
        _seed_curves_and_commands(conn, uids, buildings, now)

        # ---- 两天读数 ----
        _seed_readings(conn, sim, pmap, buildings, now)

        # ---- 班次、快照、附件 ----
        _seed_shifts(conn, uids, station_map, now)

        # ---- 历史报修与回访 ----
        _seed_history_tickets(conn, uids, buildings, residents, now)

        conn.commit()
    finally:
        conn.close()

    # ---- 实时事件链路（走服务层，确保事件/通知一并生成）----
    _seed_live_tickets(uids, buildings)


def _reset():
    conn = get_conn()
    try:
        for tbl in ("notifications", "command_tickets", "commands", "curve_versions",
                    "snapshot_readings", "snapshot_points", "snapshots",
                    "shift_attachments", "shifts", "events", "tickets",
                    "hourly_summary", "readings", "points", "residents",
                    "buildings", "stations", "users"):
            conn.execute(f"DELETE FROM {tbl}")
        conn.commit()
    finally:
        conn.close()
    for f in os.listdir(ATTACH_DIR):
        os.remove(os.path.join(ATTACH_DIR, f))


def _point(conn, sid, bid, code, kind, unit, label):
    return conn.execute(
        "INSERT INTO points(station_id, building_id, code, kind, unit, label) "
        "VALUES(?,?,?,?,?,?)", (sid, bid, code, kind, unit, label)).lastrowid


def _curve_row(conn, bid, no, pts, uid, ts, status, base=None, note="", source="manual"):
    return conn.execute(
        "INSERT INTO curve_versions(building_id, version_no, points_json, source, "
        "base_version, created_by, created_at, status, note) VALUES(?,?,?,?,?,?,?,?,?)",
        (bid, no, dumps(pts), source, base, uid, ts, status, note)).lastrowid


def _shifted(delta):
    return [{"outdoor": p["outdoor"], "supply": p["supply"] + delta,
             "return": p["return"] + delta * 0.8} for p in BASE_CURVE]


def _seed_curves_and_commands(conn, uids, buildings, now):
    t0 = (now - timedelta(days=2) + timedelta(hours=1)).isoformat(timespec="seconds")
    bid1, sid1, _ = buildings[0]
    bid2, _, _ = buildings[1]
    bid3, _, _ = buildings[2]
    bid4, _, _ = buildings[3]

    # 所有楼栋先有 v1 生效曲线
    v1 = {}
    for bid, _, _ in buildings:
        v1[bid] = _curve_row(conn, bid, 1, BASE_CURVE, uids["dispatcher1"], t0, "active")

    # 已结束指令（昨天，1号楼降温）
    old2 = _curve_row(conn, bid1, 2, _shifted(-2), uids["dispatcher1"],
                      (now - timedelta(hours=26)).isoformat(timespec="seconds"),
                      "superseded", base=1, source="command", note="午间气温回升微调")
    t_sub = (now - timedelta(hours=26)).isoformat(timespec="seconds")
    t_rev = (now - timedelta(hours=25, minutes=50)).isoformat(timespec="seconds")
    t_exec = (now - timedelta(hours=25, minutes=40)).isoformat(timespec="seconds")
    t_end = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO commands(building_id, new_version_id, old_version_id, status, breach, "
        "submitted_by, submitted_at, reviewed_by, reviewed_at, executed_by, executed_at, "
        "ended_at, effect, effect_judgement, observe_minutes) "
        "VALUES(?,?,?,'ended',0,?,?,?,?,?,?,?,?,'effective',90)",
        (bid1, old2, v1[bid1], uids["dispatcher1"], t_sub, uids["manager1"], t_rev,
         uids["ops1"], t_exec, t_end, "观察窗内无新增冷暖报修，住户回访满意"))

    # 生效观察中的指令（2号楼，40 分钟前执行）
    warm = _shifted(3)
    v2b2 = _curve_row(conn, bid2, 2, warm, uids["dispatcher2"],
                      (now - timedelta(minutes=55)).isoformat(timespec="seconds"),
                      "active", base=1, source="command", note="寒潮提前升温")
    conn.execute("UPDATE curve_versions SET status='superseded' WHERE id=?", (v1[bid2],))
    conn.execute(
        "INSERT INTO commands(building_id, new_version_id, old_version_id, status, breach, "
        "submitted_by, submitted_at, reviewed_by, reviewed_at, executed_by, executed_at, "
        "observe_minutes) VALUES(?,?,?,'observing',0,?,?,?,?,?,?,90)",
        (bid2, v2b2, v1[bid2], uids["dispatcher2"],
         (now - timedelta(minutes=55)).isoformat(timespec="seconds"),
         uids["dispatcher1"], (now - timedelta(minutes=50)).isoformat(timespec="seconds"),
         uids["ops1"], (now - timedelta(minutes=40)).isoformat(timespec="seconds")))

    # 待复核越限指令（3号楼，供水 80℃）
    breach_pts = [dict(p) for p in BASE_CURVE]
    breach_pts[0]["supply"] = 80.0
    breach_pts[0]["return"] = 62.0
    v2b3 = _curve_row(conn, bid3, 2, breach_pts, uids["dispatcher2"],
                      (now - timedelta(minutes=10)).isoformat(timespec="seconds"),
                      "proposed", base=1, source="command", note="极端寒潮预案")
    cid = conn.execute(
        "INSERT INTO commands(building_id, new_version_id, old_version_id, status, breach, "
        "breach_reason, submitted_by, submitted_at, observe_minutes) "
        "VALUES(?,?,?,'pending_review',1,?,?,?,90)",
        (bid3, v2b3, v1[bid3],
         "供水 80.0℃ > 75℃ 高限；室外 -20℃ 处供水调整 +10.0℃，超过 ±10℃ 调整上限",
         uids["dispatcher2"],
         (now - timedelta(minutes=10)).isoformat(timespec="seconds"))).lastrowid
    conn.execute(
        "INSERT INTO notifications(kind, ref_type, ref_id, message, created_at) "
        "VALUES('breach_approval','command',?,?,?)",
        (cid, f"越限指令 #{cid} 待站长批准：供水 80.0℃ > 75℃ 高限", now_iso()))

    # 已复核待执行指令（4号楼）
    mild = _shifted(-1.5)
    v2b4 = _curve_row(conn, bid4, 2, mild, uids["dispatcher1"],
                      (now - timedelta(hours=2)).isoformat(timespec="seconds"),
                      "proposed", base=1, source="command", note="阶段性降压微调")
    conn.execute(
        "INSERT INTO commands(building_id, new_version_id, old_version_id, status, breach, "
        "submitted_by, submitted_at, reviewed_by, reviewed_at, observe_minutes) "
        "VALUES(?,?,?,'pending_execution',0,?,?,?,?,90)",
        (bid4, v2b4, v1[bid4], uids["dispatcher1"],
         (now - timedelta(hours=2)).isoformat(timespec="seconds"),
         uids["manager1"], (now - timedelta(hours=1, minutes=50)).isoformat(timespec="seconds")))

    # 被驳回的指令（1号楼，前天）
    bad = _curve_row(conn, bid1, 3, _shifted(-8), uids["dispatcher2"],
                     (now - timedelta(hours=40)).isoformat(timespec="seconds"),
                     "rejected", base=2, source="command", note="申请大幅降温")
    conn.execute(
        "INSERT INTO commands(building_id, new_version_id, old_version_id, status, breach, "
        "submitted_by, submitted_at, reviewed_by, reviewed_at, review_note) "
        "VALUES(?,?,?,'rejected',1,?,?,?,?,?)",
        (bid1, bad, old2, uids["dispatcher2"],
         (now - timedelta(hours=40)).isoformat(timespec="seconds"),
         uids["manager1"], (now - timedelta(hours=39, minutes=50)).isoformat(timespec="seconds"),
         "降幅过大，驳回"))


def _seed_readings(conn, sim, pmap, buildings, now):
    start = now - timedelta(days=DAYS)
    n = int(DAYS * 24 * 60 / STEP_MIN)
    # 制造一段历史断点（已补录）和一段当前断点（未恢复，近 20 分钟）
    gap_point = pmap["HPL-4_TS"]["id"]
    hist_gap_point = pmap["HPL-3_TR"]["id"]
    state: dict[int, float] = {}
    for i in range(n + 1):
        t = start + timedelta(minutes=i * STEP_MIN)
        ts = t.isoformat(timespec="seconds")
        age_min = (now - t).total_seconds() / 60
        for sid_code, station in [(c, None) for c in ("HPL", "QH")]:
            sid = conn.execute("SELECT id FROM stations WHERE code=?", (sid_code,)).fetchone()["id"]
            outdoor = sim.outdoor_at(sid, t)
            conn.execute(
                "INSERT OR IGNORE INTO readings(point_id, ts, value) VALUES(?,?,?)",
                (pmap[f"{sid_code}_OD"]["id"], ts, round(outdoor, 2)))
            conn.execute(
                "INSERT OR IGNORE INTO readings(point_id, ts, value) VALUES(?,?,?)",
                (pmap[f"{sid_code}_PS"]["id"], ts,
                 round(0.56 + sim.rnd.uniform(-0.015, 0.015), 3)))
            conn.execute(
                "INSERT OR IGNORE INTO readings(point_id, ts, value) VALUES(?,?,?)",
                (pmap[f"{sid_code}_PR"]["id"], ts,
                 round(0.29 + sim.rnd.uniform(-0.01, 0.01), 3)))
            conn.execute(
                "INSERT OR IGNORE INTO readings(point_id, ts, value) VALUES(?,?,?)",
                (pmap[f"{sid_code}_PMP"]["id"], ts, 1.0))
            for bid, bsid, bname in buildings:
                if bsid != sid:
                    continue
                pts = conn.execute(
                    "SELECT points_json FROM curve_versions WHERE building_id=? "
                    "AND created_at<=? ORDER BY version_no DESC LIMIT 1",
                    (bid, ts)).fetchone()
                cpts = json.loads(pts["points_json"]) if pts else BASE_CURVE
                target = curves.interpolate(cpts, outdoor, "supply")
                key = bid
                actual = state.get(key, target)
                actual += (target - actual) * 0.4 + sim.rnd.uniform(-0.5, 0.5)
                state[key] = actual
                load = max(0.0, -outdoor) * 0.25
                delta = 9.5 + load + sim.rnd.uniform(-0.8, 0.8)
                sup_pid = pmap[[c for c in pmap
                                if pmap[c]["building_id"] == bid
                                and pmap[c]["kind"] == "supply_temp"][0]]["id"]
                ret_code = [c for c in pmap
                            if pmap[c]["building_id"] == bid
                            and pmap[c]["kind"] == "return_temp"][0]
                ret_pid = pmap[ret_code]["id"]
                # 当前断点：最近 20 分钟无读数
                if not (sup_pid == gap_point and age_min <= 20):
                    conn.execute(
                        "INSERT OR IGNORE INTO readings(point_id, ts, value) VALUES(?,?,?)",
                        (sup_pid, ts, round(actual, 2)))
                # 历史断点窗口（约 26 小时前，30 分钟）：该窗口读数以补录标记写入
                in_hist_gap = (ret_pid == hist_gap_point
                               and 26 * 60 - 30 <= age_min <= 26 * 60)
                conn.execute(
                    "INSERT OR IGNORE INTO readings(point_id, ts, value, backfilled) "
                    "VALUES(?,?,?,?)",
                    (ret_pid, ts, round(actual - delta, 2), 1 if in_hist_gap else 0))


def _seed_shifts(conn, uids, station_map, now):
    """两天 4 个已交接班次 + 1 个进行中班次；为历史班次拍不可覆盖快照。"""
    kinds = [("night", -2, 20, 8), ("day", -1, 8, 20),
             ("night", -1, 20, 8), ("day", 0, 8, 20)]
    seq = 0
    hist_shift_id = None
    for kind, day_off, sh, eh in kinds:
        seq += 1
        d_start = (now + timedelta(days=day_off)).replace(hour=sh, minute=0, second=0)
        if sh >= eh:
            d_end = d_start + timedelta(hours=12)
        else:
            d_end = (now + timedelta(days=day_off)).replace(hour=eh, minute=0, second=0)
        if d_end > now:
            d_end = now
        sid = station_map["HPL"]
        cur = conn.execute(
            "INSERT INTO shifts(station_id, seq, kind, operator_id, started_at, ended_at, "
            "handover_note) VALUES(?,?,?,?,?,?,?)",
            (sid, seq, kind, uids["dispatcher1"],
             d_start.isoformat(timespec="seconds"), d_end.isoformat(timespec="seconds"),
             "运行平稳，参数正常交接。" if seq % 2 else "3号楼温差偏小已跟踪，附件见交接记录。"))
        hist_shift_id = cur.lastrowid
        # 快照（冻结整班 12 小时窗口）
        snap = conn.execute(
            "INSERT INTO snapshots(shift_id, taken_at, window_start, window_end, note) "
            "VALUES(?,?,?,?,?)",
            (hist_shift_id, d_end.isoformat(timespec="seconds"),
             d_start.isoformat(timespec="seconds"), d_end.isoformat(timespec="seconds"),
             "交班快照")).lastrowid
        for p in conn.execute("SELECT * FROM points WHERE station_id=?", (sid,)).fetchall():
            rows = conn.execute(
                "SELECT ts, value, backfilled FROM readings WHERE point_id=? "
                "AND ts BETWEEN ? AND ?",
                (p["id"], d_start.isoformat(timespec="seconds"),
                 d_end.isoformat(timespec="seconds"))).fetchall()
            conn.executemany(
                "INSERT INTO snapshot_readings(snapshot_id, point_id, ts, value, backfilled) "
                "VALUES(?,?,?,?,?)",
                [(snap, p["id"], r["ts"], r["value"], r["backfilled"]) for r in rows])
            vals = [r["value"] for r in rows]
            if vals:
                conn.execute(
                    "INSERT INTO snapshot_points(snapshot_id, point_id, avg_value, "
                    "min_value, max_value, latest_value, latest_ts, sample_count, gap_count) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (snap, p["id"], round(sum(vals)/len(vals), 2), min(vals), max(vals),
                     vals[-1], rows[-1]["ts"], len(vals), 0))

    # 当前进行中班次
    seq += 1
    conn.execute(
        "INSERT INTO shifts(station_id, seq, kind, operator_id, started_at) VALUES(?,?,?,?,?)",
        (station_map["HPL"], seq, "night", uids["dispatcher2"],
         now.replace(hour=20, minute=0, second=0).isoformat(timespec="seconds")
         if now.hour >= 20 else (now - timedelta(hours=2)).isoformat(timespec="seconds")))

    # 交接附件
    stored = f"{hist_shift_id}_handover_note.txt"
    path = os.path.join(ATTACH_DIR, stored)
    with open(path, "w", encoding="utf-8") as f:
        f.write("交接附件示例\n3号楼回水温度偏低核查记录：已现场排气，温差恢复 9℃ 以上。\n")
    conn.execute(
        "INSERT INTO shift_attachments(shift_id, filename, stored_name, size, uploaded_by, "
        "uploaded_at) VALUES(?,?,?,?,?,?)",
        (hist_shift_id, "3号楼核查记录.txt", stored, os.path.getsize(path),
         uids["dispatcher1"], now_iso()))


def _seed_history_tickets(conn, uids, buildings, residents, now):
    """两天内的历史报修/处置/回访，以及观察窗内的冷暖报修。"""
    rng = SimAdapter(seed=7).rnd
    observing_cmd = conn.execute(
        "SELECT id, building_id FROM commands WHERE status='observing'").fetchone()
    descs = {"cold": "室内暖气不热，摸着温乎", "hot": "家里太热，需要开窗",
             "leak": "阀门处有渗水", "other": "咨询供暖时间"}
    k = 0
    for hours_ago, (bid, _, bname), cat in [
        (44, buildings[0], "cold"), (43, buildings[0], "cold"),
        (40, buildings[1], "leak"), (36, buildings[3], "cold"),
        (33, buildings[2], "cold"), (30, buildings[4], "other"),
        (28, buildings[0], "hot"), (24, buildings[2], "cold"),
        (22, buildings[5], "leak"), (20, buildings[1], "cold"),
        (18, buildings[3], "cold"), (15, buildings[4], "cold"),
        (12, buildings[0], "other"), (9, buildings[5], "cold"),
        (7, buildings[3], "leak"), (5, buildings[4], "cold"),
    ]:
        k += 1
        res = [r for r in residents if r[1] == bid]
        rid, _, room = rng.choice(res)
        t0 = now - timedelta(hours=hours_ago, minutes=rng.randint(0, 50))
        t1 = t0 + timedelta(minutes=rng.randint(20, 120))
        t2 = t1 + timedelta(hours=rng.randint(2, 10))
        satisfied = not (k in (5, 14))  # 两件不满意
        state = "resolved" if satisfied else "reopened"
        conn.execute(
            "INSERT INTO tickets(building_id, resident_id, room, channel, category, "
            "description, created_at, state, handled_by, handled_at, handle_note, "
            "callback_state, callback_by, callback_at, callback_note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, rid, room, rng.choice(["电话", "小程序", "上门"]), cat, descs[cat],
             t0.isoformat(timespec="seconds"), state, uids["ops1"],
             t1.isoformat(timespec="seconds"),
             f"已上门排气/平衡调节（{bname}）",
             "satisfied" if satisfied else "dissatisfied",
             uids["service1"], t2.isoformat(timespec="seconds"),
             "温度恢复" if satisfied else "仍不热，要求再次处理"))
        if not satisfied:
            conn.execute(
                "INSERT INTO events(type, severity, station_id, building_id, detected_at, "
                "status, handled_by, handled_at, resolution, payload_json) "
                "VALUES('callback_unsatisfied','warning',(SELECT station_id FROM buildings "
                "WHERE id=?),?,?, 'handled',?,?,?,?)",
                (bid, bid, t2.isoformat(timespec="seconds"),
                 uids["service1"], t2.isoformat(timespec="seconds"), "已二次派单",
                 dumps({"seed": True})))

    # 观察窗内 2 件冷报修（关联到 observing 指令）
    for mins in (35, 12):
        res = [r for r in residents if r[1] == observing_cmd["building_id"]]
        rid, _, room = rng.choice(res)
        conn.execute(
            "INSERT INTO tickets(building_id, resident_id, room, channel, category, "
            "description, created_at, state) VALUES(?,?,?,?,?,?,?,'open')",
            (observing_cmd["building_id"], rid, room, "电话", "cold",
             "升温后仍感觉不热",
             (now - timedelta(minutes=mins)).isoformat(timespec="seconds")))

    # 历史七类事件（已处置的四类仪表事件）
    sid = conn.execute("SELECT id FROM stations WHERE code='HPL'").fetchone()["id"]
    for etype, sev, hours, who, note, bid in [
        ("sensor_gap", "warning", 26, "ops1", "断点补录完成，通信模块已复位", buildings[2][0]),
        ("temp_diff_abnormal", "critical", 22, "dispatcher1", "调整流量平衡后温差恢复", buildings[2][0]),
        ("pressure_drop", "critical", 18, "dispatcher1", "补水定压，压力恢复 0.56MPa", None),
        ("pump_switch", "info", 16, "ops1", "1#泵切 2#泵，运行正常", None),
    ]:
        ts = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO events(type, severity, station_id, building_id, detected_at, "
            "status, handled_by, handled_at, resolution, payload_json) "
            "VALUES(?,?,?,?,?, 'handled',?,?,?, '{}')",
            (etype, sev, sid, bid, ts, uids[who], ts, note))


def _seed_live_tickets(uids, buildings):
    """走服务层生成当前开放事件：集中不热 + 重复报修。"""
    # 集中不热：1号楼近 30 分钟内 3 件冷报修
    bid = buildings[0][0]
    for room, mins in [("1单元301", 28), ("1单元302", 20), ("2单元405", 6)]:
        tickets.create_ticket(bid, room, "电话", "cold",
                              "整栋暖气不热，立管冰凉" if mins == 6 else "家里温度只有 17℃")
    # 重复报修：同一户 2 小时内再次报冷（第二张自动归并）
    tickets.create_ticket(buildings[3][0], "1单元303", "小程序", "cold", "还是不热（首次）")
    t2 = tickets.create_ticket(buildings[3][0], "1单元303", "电话", "cold",
                               "仍未有人上门，再次报修")
    return t2
