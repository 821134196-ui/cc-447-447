"""SQLite schema and connection helpers.

运行库位于 data/heating.db（可由 HEATING_DB 环境变量覆盖）。
读数原始表 readings 可被断点补录；每次班次快照另存冻结副本，不可覆盖。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("HEATING_DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.environ.get("HEATING_DB", os.path.join(DATA_DIR, "heating.db"))
ATTACH_DIR = os.path.join(DATA_DIR, "handover_attachments")

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(ATTACH_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        with _lock:
            yield conn
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, args=()):
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def query_one(sql: str, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('dispatcher','manager','ops','service'))
);

CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buildings (
    id INTEGER PRIMARY KEY,
    station_id INTEGER NOT NULL REFERENCES stations(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    area_m2 REAL NOT NULL,
    households INTEGER NOT NULL,
    UNIQUE(station_id, code)
);

CREATE TABLE IF NOT EXISTS residents (
    id INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    room TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    UNIQUE(building_id, room)
);

CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY,
    station_id INTEGER NOT NULL REFERENCES stations(id),
    building_id INTEGER REFERENCES buildings(id),
    code TEXT NOT NULL,
    kind TEXT NOT NULL,
    unit TEXT NOT NULL,
    label TEXT NOT NULL,
    UNIQUE(station_id, code)
);

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    point_id INTEGER NOT NULL REFERENCES points(id),
    ts TEXT NOT NULL,
    value REAL NOT NULL,
    backfilled INTEGER NOT NULL DEFAULT 0,
    UNIQUE(point_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_readings_pt ON readings(point_id, ts);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY,
    station_id INTEGER NOT NULL REFERENCES stations(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    operator_id INTEGER NOT NULL REFERENCES users(id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    handover_note TEXT DEFAULT '',
    UNIQUE(station_id, seq)
);

CREATE TABLE IF NOT EXISTS shift_attachments (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL REFERENCES shifts(id),
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    uploaded_by INTEGER NOT NULL REFERENCES users(id),
    uploaded_at TEXT NOT NULL
);

-- 不可覆盖的运行快照：冻结的读数副本与汇总
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL REFERENCES shifts(id),
    taken_at TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshot_points (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    point_id INTEGER NOT NULL REFERENCES points(id),
    avg_value REAL,
    min_value REAL,
    max_value REAL,
    latest_value REAL,
    latest_ts TEXT,
    sample_count INTEGER NOT NULL,
    gap_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshot_readings (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    point_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    value REAL NOT NULL,
    backfilled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapread ON snapshot_readings(snapshot_id, point_id);

-- 楼栋供暖曲线：版本链 + 乐观并发
CREATE TABLE IF NOT EXISTS curve_versions (
    id INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    version_no INTEGER NOT NULL,
    points_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    base_version INTEGER,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed','active','superseded','rejected')),
    note TEXT DEFAULT '',
    UNIQUE(building_id, version_no)
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    new_version_id INTEGER NOT NULL REFERENCES curve_versions(id),
    old_version_id INTEGER REFERENCES curve_versions(id),
    status TEXT NOT NULL
        CHECK(status IN ('pending_review','pending_execution','observing','ended','rejected')),
    breach INTEGER NOT NULL DEFAULT 0,
    breach_reason TEXT DEFAULT '',
    submitted_by INTEGER NOT NULL REFERENCES users(id),
    submitted_at TEXT NOT NULL,
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    review_note TEXT DEFAULT '',
    executed_by INTEGER REFERENCES users(id),
    executed_at TEXT,
    observe_minutes INTEGER NOT NULL DEFAULT 90,
    ended_at TEXT,
    effect TEXT DEFAULT '',
    effect_judgement TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cmd_status ON commands(status);

CREATE TABLE IF NOT EXISTS command_tickets (
    command_id INTEGER NOT NULL REFERENCES commands(id),
    ticket_id INTEGER NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY(command_id, ticket_id)
);

-- 七类运行事件，分类分开处置
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN (
        'sensor_gap','temp_diff_abnormal','pressure_drop',
        'pump_switch','mass_cold','repeat_repair','callback_unsatisfied')),
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
    station_id INTEGER REFERENCES stations(id),
    building_id INTEGER REFERENCES buildings(id),
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','handled')),
    handled_by INTEGER REFERENCES users(id),
    handled_at TEXT,
    resolution TEXT DEFAULT '',
    payload_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status, type);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    resident_id INTEGER REFERENCES residents(id),
    room TEXT,
    channel TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('cold','hot','leak','other')),
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open'
        CHECK(state IN ('open','processing','merged','resolved','reopened')),
    master_id INTEGER REFERENCES tickets(id),
    handled_by INTEGER REFERENCES users(id),
    handled_at TEXT,
    handle_note TEXT DEFAULT '',
    callback_state TEXT NOT NULL DEFAULT 'none'
        CHECK(callback_state IN ('none','satisfied','dissatisfied')),
    callback_by INTEGER REFERENCES users(id),
    callback_at TEXT,
    callback_note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tickets_b ON tickets(building_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_state ON tickets(state);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id INTEGER,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hourly_summary (
    id INTEGER PRIMARY KEY,
    point_id INTEGER NOT NULL REFERENCES points(id),
    hour TEXT NOT NULL,
    avg_value REAL,
    min_value REAL,
    max_value REAL,
    sample_count INTEGER NOT NULL,
    gap INTEGER NOT NULL DEFAULT 0,
    UNIQUE(point_id, hour)
);
"""


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
