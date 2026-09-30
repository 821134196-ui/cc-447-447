"""SQLite 连接与 schema。

设计要点：
- snapshots 为只增（append-only）表，触发器拒绝 UPDATE/DELETE —— 运行快照不可覆盖；
- readings 允许补录（断点补录 = 插入更早时间戳的读数，source='backfill'）；
- curve_versions 对 (station_id, building_id, version) 唯一，乐观锁在服务层校验。
"""
import json
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    address     TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buildings (
    id          INTEGER PRIMARY KEY,
    station_id  INTEGER NOT NULL REFERENCES stations(id),
    name        TEXT NOT NULL,
    units       INTEGER NOT NULL DEFAULT 1,
    households  INTEGER NOT NULL DEFAULT 0,
    area        REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS residents (
    id          INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    room        TEXT NOT NULL,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK(role IN ('dispatcher','manager','ops','service'))
);

CREATE TABLE IF NOT EXISTS shifts (
    id          INTEGER PRIMARY KEY,
    station_id  INTEGER NOT NULL REFERENCES stations(id),
    shift_date  TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    leader      TEXT NOT NULL,
    note        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS readings (
    id               INTEGER PRIMARY KEY,
    station_id       INTEGER NOT NULL REFERENCES stations(id),
    building_id      INTEGER NOT NULL REFERENCES buildings(id),
    ts               TEXT NOT NULL,
    outdoor_temp     REAL NOT NULL,
    supply_temp      REAL NOT NULL,
    return_temp      REAL NOT NULL,
    supply_pressure  REAL NOT NULL,
    return_pressure  REAL NOT NULL,
    pump_status      TEXT NOT NULL,           -- running/stopped/switching
    pump_frequency   REAL NOT NULL,
    source           TEXT NOT NULL DEFAULT 'live'  -- live / backfill / sim
);
CREATE INDEX IF NOT EXISTS idx_readings_lookup
    ON readings(station_id, building_id, ts);

-- 不可覆盖的运行快照（哈希链）
CREATE TABLE IF NOT EXISTS snapshots (
    id               INTEGER PRIMARY KEY,
    shift_id         INTEGER NOT NULL REFERENCES shifts(id),
    station_id       INTEGER NOT NULL REFERENCES stations(id),
    ts               TEXT NOT NULL,
    outdoor_temp     REAL NOT NULL,
    supply_temp      REAL NOT NULL,
    return_temp      REAL NOT NULL,
    supply_pressure  REAL NOT NULL,
    return_pressure  REAL NOT NULL,
    pump_status      TEXT NOT NULL,
    reading_count    INTEGER NOT NULL,
    readings_from    TEXT NOT NULL,
    readings_to      TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    prev_hash        TEXT NOT NULL,
    hash             TEXT NOT NULL UNIQUE
);

CREATE TRIGGER IF NOT EXISTS trg_snapshot_no_update
BEFORE UPDATE ON snapshots
BEGIN
    SELECT RAISE(ABORT, '运行快照不可覆盖：UPDATE 被禁止');
END;

CREATE TRIGGER IF NOT EXISTS trg_snapshot_no_delete
BEFORE DELETE ON snapshots
BEGIN
    SELECT RAISE(ABORT, '运行快照不可删除：DELETE 被禁止');
END;

-- 供暖曲线版本
CREATE TABLE IF NOT EXISTS curve_versions (
    id           INTEGER PRIMARY KEY,
    station_id   INTEGER NOT NULL REFERENCES stations(id),
    building_id  INTEGER NOT NULL REFERENCES buildings(id),
    version      INTEGER NOT NULL,
    -- 折点：[{outdoor, supply, return}, ...] 按 outdoor 升序
    points_json  TEXT NOT NULL,
    base_version INTEGER,
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    note         TEXT DEFAULT '',
    UNIQUE(station_id, building_id, version)
);

-- 调节指令：待复核 → 待执行 → 生效观察 → 结束
CREATE TABLE IF NOT EXISTS commands (
    id                 INTEGER PRIMARY KEY,
    station_id         INTEGER NOT NULL REFERENCES stations(id),
    building_id        INTEGER NOT NULL REFERENCES buildings(id),
    old_curve_version  INTEGER,
    new_curve_version  INTEGER,
    status             TEXT NOT NULL CHECK(status IN
                         ('pending_review','pending_execution','observing','finished','rejected')),
    over_limit         INTEGER NOT NULL DEFAULT 0,
    limit_detail       TEXT DEFAULT '',
    submitted_by       INTEGER NOT NULL REFERENCES users(id),
    submitted_at       TEXT NOT NULL,
    reviewed_by        INTEGER REFERENCES users(id),
    reviewed_at        TEXT,
    review_note        TEXT DEFAULT '',
    executed_by        INTEGER REFERENCES users(id),
    executed_at        TEXT,
    observe_until      TEXT,
    finished_at        TEXT,
    effect_note        TEXT DEFAULT ''
);

-- 居民报修
CREATE TABLE IF NOT EXISTS complaints (
    id           INTEGER PRIMARY KEY,
    station_id   INTEGER NOT NULL REFERENCES stations(id),
    building_id  INTEGER NOT NULL REFERENCES buildings(id),
    resident_id  INTEGER REFERENCES residents(id),
    kind         TEXT NOT NULL,          -- cold / hot / noise / leak / other
    content      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','merged','resolved')),
    merged_into  INTEGER REFERENCES complaints(id),
    command_id   INTEGER REFERENCES commands(id),
    source       TEXT NOT NULL DEFAULT 'phone',  -- phone / app / patrol
    created_at   TEXT NOT NULL,
    resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_complaints_bld_time
    ON complaints(building_id, created_at);

-- 处置回访
CREATE TABLE IF NOT EXISTS callbacks (
    id           INTEGER PRIMARY KEY,
    complaint_id INTEGER NOT NULL REFERENCES complaints(id),
    result       TEXT NOT NULL CHECK(result IN ('satisfied','unsatisfied')),
    note         TEXT DEFAULT '',
    operator_id  INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL
);

-- 七类运行/服务事件，分开建类、分开处置
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK(kind IN (
                    'sensor_break','temp_diff_abnormal','pressure_drop',
                    'pump_switch','mass_cold','repeat_repair','callback_unsatisfied')),
    severity     TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
    status       TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    station_id   INTEGER NOT NULL REFERENCES stations(id),
    building_id  INTEGER REFERENCES buildings(id),
    detail_json  TEXT NOT NULL DEFAULT '{}',
    related_id   INTEGER,
    started_at   TEXT NOT NULL,
    closed_at    TEXT,
    close_note   TEXT DEFAULT '',
    closed_by    INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_events_open ON events(status, kind);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY,
    level      TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    read       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS handovers (
    id              INTEGER PRIMARY KEY,
    shift_id        INTEGER NOT NULL REFERENCES shifts(id),
    station_id      INTEGER NOT NULL REFERENCES stations(id),
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL,
    outgoing_leader TEXT NOT NULL,
    incoming_leader TEXT NOT NULL,
    summary         TEXT NOT NULL,
    attachment_name TEXT DEFAULT '',
    attachment_path TEXT DEFAULT ''
);

-- 进程内汇总任务的产物
CREATE TABLE IF NOT EXISTS aggregates_hourly (
    id            INTEGER PRIMARY KEY,
    station_id    INTEGER NOT NULL REFERENCES stations(id),
    building_id   INTEGER NOT NULL REFERENCES buildings(id),
    hour          TEXT NOT NULL,          -- 'YYYY-MM-DD HH:00:00'
    reading_count INTEGER NOT NULL,
    avg_outdoor   REAL NOT NULL,
    avg_supply    REAL NOT NULL,
    avg_return    REAL NOT NULL,
    avg_pressure  REAL NOT NULL,
    UNIQUE(station_id, building_id, hour)
);
"""


def get_conn(db_path=None):
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=None):
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    if "points_json" in d and d["points_json"]:
        d["points"] = json.loads(d.pop("points_json"))
    if "detail_json" in d and d["detail_json"]:
        d["detail"] = json.loads(d.pop("detail_json"))
    return d
