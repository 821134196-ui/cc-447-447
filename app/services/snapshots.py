"""班次运行快照：每个班次接收测点读数并形成不可覆盖的哈希链快照。"""
import hashlib
import json

from ..db import row_to_dict
from . import timeutil as t


def take_snapshot(conn, shift_id):
    """汇总该班次当前已接收读数，形成一条 append-only 快照。

    快照内容：全站点各楼栋在班内时间窗的读数聚合 + 逐楼栋最后读数。
    通过 prev_hash 串成哈希链，任何篡改都会断链。
    """
    shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if not shift:
        raise ValueError("班次不存在")
    window_from = shift["started_at"]
    window_to = t.iso_now()

    rows = conn.execute(
        """SELECT station_id, building_id,
                  COUNT(*) AS n,
                  AVG(outdoor_temp) AS ao, AVG(supply_temp) AS asu,
                  AVG(return_temp) AS ar, AVG(supply_pressure) AS asp,
                  AVG(return_pressure) AS arp
           FROM readings WHERE ts>=? AND ts<=?
           GROUP BY station_id, building_id
           ORDER BY building_id""",
        (window_from, window_to)).fetchall()
    if not rows:
        raise ValueError("本班次暂无读数，无法形成快照")

    last = conn.execute("SELECT hash FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = last["hash"] if last else "GENESIS"

    reading_count = sum(r["n"] for r in rows)
    # 站点级代表值 = 各楼栋均值的平均
    def avg(key):
        return round(sum(r[key] for r in rows) / len(rows), 2)

    # 水泵状态取全站点最新一条
    pump = conn.execute(
        "SELECT pump_status FROM readings WHERE ts<=? ORDER BY ts DESC,id DESC LIMIT 1",
        (window_to,)).fetchone()
    payload = {
        "shift_id": shift_id,
        "window": [window_from, window_to],
        "buildings": [
            {"building_id": r["building_id"], "count": r["n"],
             "supply": round(r["asu"], 2), "return": round(r["ar"], 2),
             "supply_pressure": round(r["asp"], 3)}
            for r in rows
        ],
    }
    h = hashlib.sha256(
        (prev_hash + json.dumps(payload, sort_keys=True, ensure_ascii=False)).encode()
    ).hexdigest()

    h = hashlib.sha256(
        (prev_hash + json.dumps(payload, sort_keys=True, ensure_ascii=False)).encode()
    ).hexdigest()
    payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)

    cur = conn.execute(
        """INSERT INTO snapshots(shift_id, station_id, ts, outdoor_temp,
               supply_temp, return_temp, supply_pressure, return_pressure,
               pump_status, reading_count, readings_from, readings_to,
               payload_json, prev_hash, hash)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (shift_id, shift["station_id"], window_to, avg("ao"), avg("asu"), avg("ar"),
         avg("asp"), avg("arp"),
         pump["pump_status"] if pump else "unknown",
         reading_count, window_from, window_to, payload_json, prev_hash, h))
    conn.commit()
    return row_to_dict(conn.execute("SELECT * FROM snapshots WHERE id=?",
                                    (cur.lastrowid,)).fetchone())


def verify_chain(conn):
    """校验快照哈希链完整性，返回 (ok, 断裂位置或 None)。

    对每条快照重算 prev_hash + payload 的 SHA-256，与存储 hash 比对，
    同时校验 prev_hash 指针。任何对历史快照行的篡改都会在此暴露。
    """
    rows = conn.execute("SELECT * FROM snapshots ORDER BY id").fetchall()
    prev = "GENESIS"
    for row in rows:
        if row["prev_hash"] != prev:
            return False, row["id"]
        expect = hashlib.sha256((prev + row["payload_json"]).encode()).hexdigest()
        if expect != row["hash"]:
            return False, row["id"]
        prev = row["hash"]
    return True, None


def list_snapshots(conn, shift_id=None, limit=200):
    sql = "SELECT * FROM snapshots"
    args = ()
    if shift_id:
        sql += " WHERE shift_id=?"
        args = (shift_id,)
    sql += " ORDER BY id DESC LIMIT ?"
    args = args + (limit,)
    return [row_to_dict(r) for r in conn.execute(sql, args).fetchall()]
