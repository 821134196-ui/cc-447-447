"""运行班次与交接：班次开收、交接记录、附件保存到本地目录。"""
import os
import re

from .. import config
from ..db import row_to_dict
from . import timeutil as t


def open_shift(conn, station_id, leader, note="", started_at=None):
    started_at = started_at or t.iso_now()
    cur = conn.execute(
        """INSERT INTO shifts(station_id, shift_date, started_at, leader, note)
           VALUES(?,?,?,?,?)""",
        (station_id, started_at[:10], started_at, leader, note))
    conn.commit()
    return row_to_dict(conn.execute("SELECT * FROM shifts WHERE id=?",
                                    (cur.lastrowid,)).fetchone())


def close_shift(conn, shift_id, note=""):
    conn.execute("UPDATE shifts SET ended_at=?, note=? WHERE id=?",
                 (t.iso_now(), note, shift_id))
    conn.commit()


def active_shift(conn, station_id):
    return row_to_dict(conn.execute(
        "SELECT * FROM shifts WHERE station_id=? AND ended_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (station_id,)).fetchone())


def list_shifts(conn, station_id, limit=30):
    return [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM shifts WHERE station_id=? ORDER BY id DESC LIMIT ?",
        (station_id, limit)).fetchall()]


def create_handover(conn, shift_id, station_id, user, incoming_leader,
                    summary, attachment_name=None, attachment_bytes=None):
    """保存交接；附件写入本地交接目录（不入库）。"""
    shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if not shift:
        raise ValueError("班次不存在")
    safe_name, stored_path = "", ""
    if attachment_name and attachment_bytes is not None:
        safe_name = re.sub(r"[^\w.\-一-龥]", "_", attachment_name)
        stored_name = f"shift{shift_id}_{safe_name}"
        stored_path = os.path.join(config.ATTACH_DIR, stored_name)
        with open(stored_path, "wb") as f:
            f.write(attachment_bytes)
    cur = conn.execute(
        """INSERT INTO handovers(shift_id, station_id, created_by, created_at,
               outgoing_leader, incoming_leader, summary,
               attachment_name, attachment_path)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (shift_id, station_id, user["id"], t.iso_now(), shift["leader"],
         incoming_leader, summary, safe_name, stored_path))
    conn.commit()
    return row_to_dict(conn.execute("SELECT * FROM handovers WHERE id=?",
                                    (cur.lastrowid,)).fetchone())


def read_attachment(path):
    if not path or not os.path.abspath(path).startswith(
            os.path.abspath(config.ATTACH_DIR)):
        return None
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def list_handovers(conn, station_id, limit=30):
    return [row_to_dict(r) for r in conn.execute(
        """SELECT h.*, u.display_name AS creator FROM handovers h
           JOIN users u ON u.id=h.created_by
           WHERE h.station_id=? ORDER BY h.id DESC LIMIT ?""",
        (station_id, limit)).fetchall()]
