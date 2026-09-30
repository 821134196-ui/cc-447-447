"""预置两天演示数据：站点、楼栋、住户、班次快照、读数、曲线、指令、报修、回访、事件。

仅在数据库为空时执行（幂等）。
"""
import hashlib
import json
import math
import os
from datetime import timedelta

from . import config
from .db import get_conn, init_db, row_to_dict
from .services import timeutil as t
from .services import readings as reading_svc
from .services import curves as curve_svc
from .services import complaints as cmp_svc
from .services import events as event_svc
from .services import shifts as shift_svc

DEFAULT_CURVE = [
    {"outdoor": -20, "supply": 60, "return": 48},
    {"outdoor": -10, "supply": 52, "return": 42},
    {"outdoor": 0, "supply": 44, "return": 36},
    {"outdoor": 5, "supply": 40, "return": 33},
]

V2 = {
    1: [{"outdoor": -20, "supply": 62, "return": 49},
        {"outdoor": -10, "supply": 54, "return": 43},
        {"outdoor": 0, "supply": 45, "return": 37},
        {"outdoor": 5, "supply": 41, "return": 34}],
    2: [{"outdoor": -20, "supply": 58, "return": 46},
        {"outdoor": -10, "supply": 50, "return": 41},
        {"outdoor": 0, "supply": 43, "return": 36},
        {"outdoor": 5, "supply": 39, "return": 33}],
    # 3 号楼越限曲线（供水 > 75℃），指令必须经理批准
    3: [{"outdoor": -20, "supply": 78, "return": 52},
        {"outdoor": -10, "supply": 72, "return": 48},
        {"outdoor": 0, "supply": 64, "return": 42},
        {"outdoor": 5, "supply": 60, "return": 39}],
    4: [{"outdoor": -20, "supply": 59, "return": 47},
        {"outdoor": -10, "supply": 51, "return": 41},
        {"outdoor": 0, "supply": 44, "return": 36},
        {"outdoor": 5, "supply": 40, "return": 34}],
}

SURNAMES = list("王李张刘陈杨赵黄周吴徐孙马朱胡郭何林高梁")
GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "洋", "艳", "勇", "军",
         "杰", "娟", "涛", "明", "超", "秀兰", "霞", "平", "刚", "桂英"]


def seed(db_path=None):
    db_path = db_path or config.DB_PATH
    init_db(db_path)
    conn = get_conn(db_path)
    try:
        if conn.execute("SELECT COUNT(*) AS n FROM stations").fetchone()["n"]:
            return False

        now = t.now()
        def ago(h, m=0):
            return t.fmt(now - timedelta(hours=h, minutes=m))

        # ---- 用户 --------------------------------------------------------
        users = [
            ("zhang", "张建国", "dispatcher"),
            ("li", "李雪", "dispatcher"),
            ("wang", "王磊", "manager"),
            ("sun", "孙涛", "ops"),
            ("zhou", "周敏", "service"),
        ]
        uid = {}
        for i, (uname, dname, role) in enumerate(users, start=1):
            conn.execute(
                "INSERT INTO users(id,username,display_name,role) VALUES(?,?,?,?)",
                (i, uname, dname, role))
            uid[uname] = i

        # ---- 站点 / 楼栋 / 住户 ------------------------------------------
        conn.execute(
            "INSERT INTO stations(id,name,address,created_at) VALUES(?,?,?,?)",
            (1, "滨河花园换热站", "滨河路 88 号", ago(50)))
        bld_info = [
            (1, "1 号楼", 4, 96, 8200),
            (2, "2 号楼", 5, 120, 10400),
            (3, "3 号楼", 6, 144, 12600),
            (4, "4 号楼", 4, 88, 7600),
        ]
        for bid, name, units, hh, area in bld_info:
            conn.execute(
                "INSERT INTO buildings(id,station_id,name,units,households,area) "
                "VALUES(?,?,?,?,?,?)", (bid, 1, name, units, hh, area))
        rid = 1
        residents = {}  # building -> [resident_id,...]
        for bid, _, units, hh, _ in bld_info:
            residents[bid] = []
            for u_ in range(units + 1):       # 每栋取 5 户样本
                floor = (u_ % 6) + 1
                room = f"{u_+1}单元{floor:02d}0{(u_%3)+1}"
                name = SURNAMES[(rid * 3) % len(SURNAMES)] + GIVEN[rid % len(GIVEN)]
                phone = f"138{rid:04d}{(rid*7)%10000:04d}"[:11]
                conn.execute(
                    "INSERT INTO residents(id,building_id,room,name,phone) "
                    "VALUES(?,?,?,?,?)", (rid, bid, room, name, phone))
                residents[bid].append(rid)
                rid += 1
        conn.commit()

        # ---- 曲线版本 -----------------------------------------------------
        def save_ver(bid, points, ver, at, by, note):
            conn.execute(
                """INSERT INTO curve_versions(station_id,building_id,version,
                       points_json,base_version,created_by,created_at,note)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (1, bid, ver, json.dumps(points, ensure_ascii=False),
                 ver - 1 if ver > 1 else None, by, at, note))

        for bid in (1, 2, 3, 4):
            save_ver(bid, DEFAULT_CURVE, 1, ago(49), uid["zhang"], "初始供暖曲线")
        save_ver(1, V2[1], 2, ago(25, 10), uid["zhang"], "寒潮上调 2℃")
        save_ver(2, V2[2], 2, ago(2, 10), uid["li"], "远端不热微调")
        save_ver(3, V2[3], 2, ago(0, 40), uid["zhang"], "尝试提高供水（越限）")
        save_ver(4, V2[4], 2, ago(3), uid["li"], "常规平衡调整")

        # ---- 两天读数（每 10 分钟一条）-----------------------------------
        # 4 号楼在 7h~6h 前制造 1 小时断点
        # 实际供水只跟随"已执行/已生效"的指令版本，草稿与待审批版本不影响实际参数：
        #   1 号楼 v2 于 24.8h 前执行生效；2 号楼 v2 于 2h 前执行（观察中）；
        #   3 号楼越限 v2 尚未批准、4 号楼 v2 待执行 → 两楼实际始终按 v1。
        def effective_points(bid, minutes):
            use_v2 = (bid == 1 and minutes <= 24.8 * 60) or \
                     (bid == 2 and minutes <= 2 * 60)
            return V2[bid] if use_v2 else DEFAULT_CURVE

        total_min = 48 * 60
        idx = 0
        for minutes in range(total_min, 5, -10):
            ts_dt = now - timedelta(minutes=minutes)
            ts = t.fmt(ts_dt)
            # 昼夜相位：凌晨最冷、午后最暖
            phase = 2 * math.pi * ((ts_dt.hour + ts_dt.minute / 60 - 3) / 24)
            outdoor = round(-8 + 5 * math.sin(phase) - idx * 0.0, 1)
            for bid in (1, 2, 3, 4):
                if bid == 4 and 420 >= minutes >= 360:
                    continue  # 断点窗口
                pts = effective_points(bid, minutes)
                ts_, tr_ = curve_svc.interpolate(pts, outdoor)
                sup = ts_ + 0.5 * math.sin(idx * 0.5 + bid)
                ret = tr_ + 0.4 * math.cos(idx * 0.4 + bid)
                sp = 0.62 + 0.01 * math.sin(idx * 0.3 + bid)
                rp = 0.28
                pump, freq = "running", 38.0
                # 3 号楼 20h 前压力骤降
                if bid == 3 and 1205 >= minutes >= 1195:
                    sp = 0.41
                # 2 号楼 26h 前水泵切换（一个采样点）
                if bid == 2 and minutes == 1560:
                    pump, freq = "switching", 24.0
                # 3 号楼 3h 前温差异常，随后恢复
                if bid == 3 and 190 >= minutes >= 150:
                    ret = sup - 3.2
                reading_svc.add_reading(
                    conn, 1, bid, ts=ts,
                    outdoor_temp=outdoor, supply_temp=round(sup, 2),
                    return_temp=round(ret, 2),
                    supply_pressure=round(sp, 3), return_pressure=rp,
                    pump_status=pump, pump_frequency=freq, source="sim",
                    quiet=False)
            idx += 1

        # ---- 班次与快照 ---------------------------------------------------
        # 6 个历史班次（8 小时一班）+ 当前班次
        shifts_def = [(48, 40), (40, 32), (32, 24), (24, 16), (16, 8), (8, 0)]
        shift_ids = []
        leaders = ["张建国", "李雪", "张建国", "李雪", "张建国", "李雪"]
        for n, (a, b) in enumerate(shifts_def):
            cur = conn.execute(
                """INSERT INTO shifts(station_id,shift_date,started_at,ended_at,
                       leader,note) VALUES(?,?,?,?,?,?)""",
                (1, ago(a)[:10], ago(a), ago(b), leaders[n],
                 "运行平稳" if n % 2 == 0 else "处置报修 2 单"))
            shift_ids.append(cur.lastrowid)
        active = shift_svc.open_shift(conn, 1, "张建国", "当前班次", started_at=ago(8))

        def seed_snapshot(shift_id, w_from, w_to):
            rows = conn.execute(
                """SELECT building_id, COUNT(*) n, AVG(outdoor_temp) ao,
                          AVG(supply_temp) asu, AVG(return_temp) ar,
                          AVG(supply_pressure) asp
                   FROM readings WHERE ts>=? AND ts<=?
                   GROUP BY building_id ORDER BY building_id""",
                (w_from, w_to)).fetchall()
            if not rows:
                return
            last = conn.execute(
                "SELECT hash FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else "GENESIS"
            payload = {"shift_id": shift_id, "window": [w_from, w_to],
                       "buildings": [
                           {"building_id": r["building_id"], "count": r["n"],
                            "supply": round(r["asu"], 2),
                            "return": round(r["ar"], 2),
                            "supply_pressure": round(r["asp"], 3)}
                           for r in rows]}
            pj = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            h = hashlib.sha256((prev + pj).encode()).hexdigest()
            conn.execute(
                """INSERT INTO snapshots(shift_id,station_id,ts,outdoor_temp,
                       supply_temp,return_temp,supply_pressure,return_pressure,
                       pump_status,reading_count,readings_from,readings_to,
                       payload_json,prev_hash,hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (shift_id, 1, w_to,
                 round(sum(r["ao"] for r in rows) / len(rows), 2),
                 round(sum(r["asu"] for r in rows) / len(rows), 2),
                 round(sum(r["ar"] for r in rows) / len(rows), 2),
                 round(sum(r["asp"] for r in rows) / len(rows), 3), 0.28,
                 "running", sum(r["n"] for r in rows), w_from, w_to, pj, prev, h))

        for n, (a, b) in enumerate(shifts_def):
            seed_snapshot(shift_ids[n], ago(a), ago(b))
        # 当前班次真实快照（走服务，追加在哈希链尾）
        snap = _safe_snapshot(conn, active["id"])

        # ---- 调节指令 ------------------------------------------------------
        conn.execute(
            """INSERT INTO commands(station_id,building_id,old_curve_version,
                   new_curve_version,status,over_limit,limit_detail,
                   submitted_by,submitted_at,reviewed_by,reviewed_at,review_note,
                   executed_by,executed_at,observe_until,finished_at,effect_note)
               VALUES(1,1,1,2,'finished',0,'',?,?,?,?,'复核通过',?,?,?,?,'效果良好：观察期内无冷暖报修')""",
            (uid["zhang"], ago(25, 5), uid["li"], ago(25),
             uid["sun"], ago(24, 50), ago(23, 20), ago(23, 20)))
        # 2 号楼：观察期刚结束，等待值班员判定
        conn.execute(
            """INSERT INTO commands(station_id,building_id,old_curve_version,
                   new_curve_version,status,over_limit,submitted_by,submitted_at,
                   reviewed_by,reviewed_at,review_note,executed_by,executed_at,
                   observe_until)
               VALUES(1,2,1,2,'observing',0,?,?,?,?,'复核通过',?,?,?)""",
            (uid["li"], ago(2, 5), uid["zhang"], ago(2),
             uid["sun"], ago(2), ago(0, 30)))
        # 3 号楼：越限指令待复核（必须经理批准）
        conn.execute(
            """INSERT INTO commands(station_id,building_id,old_curve_version,
                   new_curve_version,status,over_limit,limit_detail,
                   submitted_by,submitted_at)
               VALUES(1,3,1,2,'pending_review',1,
               '室外-20℃折点供水78℃超出[38,75]；室外-10℃折点供水72℃…',?,?)""",
            (uid["zhang"], ago(0, 30)))
        # 4 号楼：已批准待运维执行
        conn.execute(
            """INSERT INTO commands(station_id,building_id,old_curve_version,
                   new_curve_version,status,over_limit,submitted_by,submitted_at,
                   reviewed_by,reviewed_at,review_note)
               VALUES(1,4,1,2,'pending_execution',0,?,?,?,?,'复核通过')""",
            (uid["li"], ago(2, 50), uid["zhang"], ago(2, 40)))
        conn.commit()

        # ---- 报修与回访 ----------------------------------------------------
        # 昨晚集中不热：1/2/3 号楼 40 分钟内各 1 单
        cmp_svc.create_complaint(conn, 1, 1, "cold", "客厅暖气整体不热，阀门已全开",
                                 residents[1][0], source="phone", created_at=ago(23))
        cmp_svc.create_complaint(conn, 1, 2, "cold", "卧室两组暖气片冰凉",
                                 residents[2][1], source="app", created_at=ago(22, 40))
        cmp_svc.create_complaint(conn, 1, 3, "cold", "整户不热，楼下邻居也冷",
                                 residents[3][2], source="phone", created_at=ago(22, 20))
        # 2 号楼观察期内的不热单（用于指令效果判断）
        c_obs, _, _ = cmp_svc.create_complaint(
            conn, 1, 2, "cold", "调节后仍不热，室温 17℃",
            residents[2][3], source="app", created_at=ago(1, 30))
        # 1 号楼重复报修：同住户两次
        c1, _, _ = cmp_svc.create_complaint(
            conn, 1, 1, "cold", "暖气不热，请上门",
            residents[1][1], created_at=ago(10))
        cmp_svc.create_complaint(
            conn, 1, 1, "cold", "还是不热，要求尽快处理",
            residents[1][1], created_at=ago(2))
        # 3 号楼漏水报修 + 不满意回访
        c2, _, _ = cmp_svc.create_complaint(
            conn, 1, 3, "leak", "楼道立管接口渗水",
            residents[3][0], created_at=ago(5))
        cmp_svc.resolve(conn, c1["id"], {"id": uid["sun"]})
        cmp_svc.resolve(conn, c2["id"], {"id": uid["sun"]})
        cmp_svc.add_callback(conn, c1["id"], "satisfied", "已热，感谢",
                             {"id": uid["zhou"]})
        cmp_svc.add_callback(conn, c2["id"], "unsatisfied", "仍有缓慢渗水",
                             {"id": uid["zhou"]})
        # 若干满意回访
        for n, (bid, rr) in enumerate([(2, 2), (3, 3), (1, 3), (4, 0), (2, 0)]):
            cc, _, _ = cmp_svc.create_complaint(
                conn, 1, bid, ["noise", "hot", "other", "cold", "cold"][n],
                ["管道异响", "室内过热开窗", "阀门松动", "暖气一半不热", "效果一般"][n],
                residents[bid][rr], created_at=ago(30 + n * 3))
            cmp_svc.resolve(conn, cc["id"], {"id": uid["sun"]})
            cmp_svc.add_callback(conn, cc["id"], "satisfied", "处理满意",
                                 {"id": uid["zhou"]})

        # ---- 事件处置：关闭已恢复的水泵切换 -------------------------------
        ev = conn.execute(
            "SELECT id FROM events WHERE kind='pump_switch' ORDER BY id LIMIT 1"
        ).fetchone()
        if ev:
            event_svc.close_event(conn, ev["id"], {"id": uid["sun"]},
                                  "备用泵启动正常，频率 38Hz，切换原因为主泵例行检修")

        # ---- 班次交接记录 + 本地附件 --------------------------------------
        for n, sid in enumerate(shift_ids[:3]):
            att = f"交接记录_班次{sid}.txt".encode("utf-8")
            content = (f"班次 {sid} 交接附件\n当班读数汇总正常，报修已处置。\n").encode()
            shift_svc.create_handover(
                conn, sid, 1, {"id": uid["zhang"]},
                leaders[(n + 1) % len(leaders)],
                f"设备运行正常，报修 {'无' if n else '2 单已处置'}，注意 3 号楼压力",
                attachment_name=f"交接记录_班次{sid}.txt",
                attachment_bytes=content)

        reading_svc.aggregate_hourly(conn)
        conn.commit()
        return True
    finally:
        conn.close()


def _safe_snapshot(conn, shift_id):
    from .services.snapshots import take_snapshot
    try:
        return take_snapshot(conn, shift_id)
    except ValueError:
        return None


if __name__ == "__main__":
    ok = seed()
    print("seeded" if ok else "already seeded")
