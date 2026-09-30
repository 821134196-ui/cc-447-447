"""测试公共工具：每个用例使用独立临时数据库。"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="hss-test-")
os.environ["HSS_DATA_DIR"] = _TMP
os.environ["HSS_DB"] = os.path.join(_TMP, "test.db")

from app.db import get_conn, init_db  # noqa: E402
from app.services import timeutil as t  # noqa: E402

USERS = [
    (1, "zhang", "张建国", "dispatcher"),
    (2, "li", "李雪", "dispatcher"),
    (3, "wang", "王磊", "manager"),
    (4, "sun", "孙涛", "ops"),
    (5, "zhou", "周敏", "service"),
]

CURVE = [
    {"outdoor": -20, "supply": 60, "return": 48},
    {"outdoor": -10, "supply": 52, "return": 42},
    {"outdoor": 0, "supply": 44, "return": 36},
]
OVER_CURVE = [
    {"outdoor": -20, "supply": 79, "return": 52},
    {"outdoor": -10, "supply": 72, "return": 48},
    {"outdoor": 0, "supply": 64, "return": 42},
]


def fresh_db(path=None):
    path = path or os.path.join(_TMP, "test.db")
    if os.path.exists(path):
        os.remove(path)
    init_db(path)
    conn = get_conn(path)
    for uid, uname, dname, role in USERS:
        conn.execute(
            "INSERT INTO users(id,username,display_name,role) VALUES(?,?,?,?)",
            (uid, uname, dname, role))
    conn.execute(
        "INSERT INTO stations(id,name,address,created_at) VALUES(1,'测试站','测试路',?)",
        (t.iso_now(),))
    for bid, name in ((1, "1号楼"), (2, "2号楼"), (3, "3号楼")):
        conn.execute(
            "INSERT INTO buildings(id,station_id,name,units,households,area) "
            "VALUES(?,1,?,2,48,4000)", (bid, name))
    for rid, (bid, room) in enumerate(
            [(1, "101"), (1, "102"), (2, "201"), (3, "301")], start=1):
        conn.execute(
            "INSERT INTO residents(id,building_id,room,name,phone) "
            "VALUES(?,?,?,?,?)", (rid, bid, room, f"住户{rid}", f"1380000{rid:04d}"))
    from datetime import timedelta
    conn.execute(
        "INSERT INTO shifts(id,station_id,shift_date,started_at,leader) "
        "VALUES(1,1,?,?, '张建国')",
        (t.iso_now()[:10], t.fmt(t.now() - timedelta(hours=3))))
    conn.commit()
    return conn
