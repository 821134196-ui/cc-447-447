"""零依赖 HTTP 服务：静态前端 + JSON API。"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import auth, commands, curves, monitor, readings, shifts, tickets
from .db import now_iso, query, query_one
from .readings import latest_readings, series

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "HeatingStation/1.0"

    # ---------- helpers ----------
    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _user(self):
        return auth.current_user(self.headers)

    def _body(self):
        # 仅在 JSON 请求上预解析；二进制（附件上传）由路由自行读取 rfile
        if "application/json" not in self.headers.get("Content-Type", ""):
            return {}
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ValueError("请求体不是合法 JSON")

    def _require(self, *roles):
        user = self._user()
        return auth.require(user, *roles)

    def _error(self, exc):
        if isinstance(exc, PermissionError):
            self._send_json({"error": str(exc)}, 403)
        elif isinstance(exc, FileNotFoundError):
            self._send_json({"error": "不存在"}, 404)
        elif isinstance(exc, ValueError):
            from .curves import VersionConflict
            self._send_json({"error": str(exc)},
                            409 if isinstance(exc, VersionConflict) else 400)
        else:
            self._send_json({"error": f"服务器错误：{exc}"}, 500)

    def log_message(self, fmt, *args):
        pass  # 静默；需要时自行打开

    # ---------- routing ----------
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path in STATIC:
                return self._serve_static(path)
            if path.startswith("/api/") and not self._user():
                return self._send_json({"error": "未登录"}, 403)
            self.route_get(path, qs)
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path.startswith("/api/") and path != "/api/login" and not self._user():
                # 仍需消费请求体，避免连接复用问题
                self._body()
                return self._send_json({"error": "未登录"}, 403)
            self.route_post(path, self._body())
        except Exception as exc:
            self._error(exc)

    def _serve_static(self, path):
        fname, ctype = STATIC[path]
        fpath = os.path.join(WEB_DIR, fname)
        with open(fpath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- GET ----------
    def route_get(self, path, qs):
        def q(name, default=None):
            return qs.get(name, [default])[0]

        if path == "/api/me":
            return self._send_json({"user": self._user()})
        if path == "/api/stations":
            return self._send_json({"stations": query("SELECT * FROM stations")})
        if path == "/api/buildings":
            sid = q("station_id")
            sql = ("SELECT b.*, s.name AS station_name FROM buildings b "
                   "JOIN stations s ON s.id=b.station_id")
            args = ()
            if sid:
                sql += " WHERE b.station_id=?"
                args = (sid,)
            sql += " ORDER BY b.station_id, b.code"
            return self._send_json({"buildings": query(sql, args)})
        if path == "/api/points":
            return self._send_json({"points": query(
                "SELECT p.*, b.name AS building_name FROM points p "
                "LEFT JOIN buildings b ON b.id=p.building_id "
                "WHERE (? IS NULL OR p.station_id=?) ORDER BY p.code",
                (q("station_id"), q("station_id")))})
        if path == "/api/overview":
            sid = int(q("station_id"))
            rows = latest_readings(sid)
            counts = {
                r["type"]: r["c"] for r in query(
                    "SELECT type, COUNT(*) c FROM events WHERE status='open' GROUP BY type")}
            cmd_counts = {
                r["status"]: r["c"] for r in query(
                    "SELECT status, COUNT(*) c FROM commands GROUP BY status")}
            open_tickets = query_one(
                "SELECT COUNT(*) c FROM tickets WHERE state IN ('open','reopened')")["c"]
            shift = shifts.active_shift(sid)
            return self._send_json({
                "readings": rows, "event_counts": counts,
                "command_counts": cmd_counts, "open_tickets": open_tickets,
                "active_shift": shift})
        if path == "/api/series":
            ids = [int(x) for x in q("points", "").split(",") if x]
            data = series(ids, q("start"), q("end"))
            return self._send_json({"series": data})
        if path.startswith("/api/buildings/") and path.endswith("/curves"):
            bid = int(path.split("/")[3])
            versions = []
            for v in curves.list_versions(bid):
                vv = dict(v)
                vv["points"] = json.loads(vv.pop("points_json"))
                versions.append(vv)
            return self._send_json({"versions": versions})
        if path == "/api/commands":
            return self._send_json({"commands": commands.list_commands(
                q("status"), int(q("building_id")) if q("building_id") else None)})
        if path.startswith("/api/commands/"):
            return self._send_json(commands.detail(int(path.rsplit("/", 1)[1])))
        if path == "/api/tickets":
            return self._send_json({"tickets": tickets.list_tickets(
                q("state"), int(q("building_id")) if q("building_id") else None,
                self._user())})
        if path.startswith("/api/tickets/") and path != "/api/tickets/density":
            t = tickets.get_ticket(int(path.rsplit("/", 1)[1]), self._user())
            if not t:
                raise FileNotFoundError("报修单不存在")
            return self._send_json(t)
        if path == "/api/tickets/density":
            return self._send_json({"rows": tickets.density_by_building(
                int(q("hours")) if q("hours") else 24)})
        if path == "/api/events":
            return self._send_json({"events": monitor.list_events(q("status"), q("type"))})
        if path.startswith("/api/shifts/") and path.endswith("/attachments"):
            shift_id = int(path.split("/")[3])
            return self._send_json({"attachments": shifts.list_attachments(shift_id)})
        if path.startswith("/api/shifts/") and path.endswith("/snapshots"):
            shift_id = int(path.split("/")[3])
            return self._send_json({"snapshots": query(
                "SELECT id, taken_at, window_start, window_end, note FROM snapshots "
                "WHERE shift_id=? ORDER BY taken_at", (shift_id,))})
        if path.startswith("/api/attachments/"):
            return self._download_attachment(int(path.rsplit("/", 1)[1]))
        if path.startswith("/api/snapshots/"):
            data = shifts.snapshot_detail(int(path.rsplit("/", 1)[1]))
            if not data:
                raise FileNotFoundError("快照不存在")
            return self._send_json(data)
        if path.startswith("/api/stations/") and path.endswith("/shifts"):
            sid = int(path.split("/")[3])
            return self._send_json({"shifts": shifts.list_shifts(sid)})
        if path == "/api/stats/callback":
            return self._send_json({"stats": _callback_stats()})
        if path == "/api/notifications":
            return self._send_json({"notifications": query(
                "SELECT * FROM notifications WHERE read=0 ORDER BY created_at DESC LIMIT 50")})
        self._send_json({"error": "未知接口"}, 404)

    def _download_attachment(self, attachment_id: int):
        row = query_one("SELECT * FROM shift_attachments WHERE id=?", (attachment_id,))
        if not row:
            raise FileNotFoundError("附件不存在")
        path = shifts.attachment_path(row["stored_name"])
        if not path:
            raise FileNotFoundError("附件文件缺失")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{row['filename']}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- POST ----------
    def route_post(self, path, body):
        if path == "/api/login":
            res = auth.login(body.get("username", ""))
            if not res:
                return self._send_json({"error": "用户不存在"}, 401)
            token, user = res
            return self._send_json({"token": token, "user": dict(user)})
        if path == "/api/logout":
            t = self.headers.get("Authorization", "")[7:]
            auth.logout(t)
            return self._send_json({"ok": True})
        if path == "/api/notifications/read":
            self._require()
            from .db import tx
            with tx() as conn:
                conn.execute("UPDATE notifications SET read=1")
            return self._send_json({"ok": True})

        user = self._require()

        if path.startswith("/api/buildings/") and path.endswith("/commands"):
            bid = int(path.split("/")[3])
            self._require("dispatcher", "manager")
            detail = commands.create_command(
                bid, body["points"], user, body.get("base_version"),
                body.get("note", ""))
            return self._send_json(detail, 201)
        if path.startswith("/api/commands/"):
            cid = int(path.split("/")[3])
            action = path.rsplit("/", 1)[1]
            if action == "review":
                self._require("dispatcher", "manager")
                return self._send_json(commands.review_command(
                    cid, user, bool(body.get("approve")), body.get("note", "")))
            if action == "execute":
                return self._send_json(commands.execute_command(cid, user))
            if action == "end":
                self._require("dispatcher", "manager")
                return self._send_json(commands.end_command(
                    cid, user, body.get("judgement", "uncertain"),
                    body.get("note", "")))
        if path == "/api/tickets":
            self._require("service", "dispatcher", "manager")
            t = tickets.create_ticket(
                int(body["building_id"]), body.get("room", ""),
                body.get("channel", "电话"), body.get("category", "other"),
                body.get("description", ""), body.get("resident_id"))
            return self._send_json(tickets.get_ticket(t["id"], user), 201)
        if path.startswith("/api/tickets/"):
            tid = int(path.split("/")[3])
            action = path.rsplit("/", 1)[1]
            if action == "handle":
                self._require("service", "ops", "dispatcher", "manager")
                return self._send_json(tickets.handle_ticket(
                    tid, user, body.get("note", "")))
            if action == "resolve":
                self._require("ops", "dispatcher", "manager")
                return self._send_json(tickets.resolve_ticket(
                    tid, user, body.get("note", "")))
            if action == "callback":
                self._require("service")
                return self._send_json(tickets.callback(
                    tid, user, bool(body.get("satisfied")), body.get("note", "")))
        if path.startswith("/api/events/") and path.endswith("/handle"):
            eid = int(path.split("/")[3])
            return self._send_json(monitor.handle_event(
                eid, user, body.get("resolution", "")))
        if path.startswith("/api/points/") and path.endswith("/backfill"):
            pid = int(path.split("/")[3])
            self._require("ops", "manager")
            return self._send_json(readings.backfill_reading(
                pid, body["ts"], float(body["value"]), user), 201)
        if path.startswith("/api/stations/"):
            sid = int(path.split("/")[3])
            action = path.rsplit("/", 1)[1]
            if action == "shifts-open":
                self._require("dispatcher")
                return self._send_json(shifts.open_shift(
                    sid, body.get("kind", "day"), user), 201)
            if action == "shifts-close":
                self._require("dispatcher")
                return self._send_json(shifts.close_shift(
                    sid, user, body.get("note", "")))
            if action == "snapshots":
                self._require("dispatcher")
                return self._send_json(shifts.take_snapshot(
                    sid, user, body.get("note", ""),
                    int(body.get("window_minutes", 60))), 201)
        if path.startswith("/api/shifts/") and path.endswith("/attachments"):
            shift_id = int(path.split("/")[3])
            self._require("dispatcher", "manager")
            filename = self.headers.get("X-Filename", "attachment.bin")
            n = int(self.headers.get("Content-Length", 0) or 0)
            data = self.rfile.read(n) if n else b""
            return self._send_json(
                shifts.add_attachment(shift_id, filename, data, user), 201)
        self._send_json({"error": "未知接口"}, 404)


def _callback_stats():
    rows = query(
        """
        SELECT callback_state, COUNT(*) c FROM tickets
        WHERE callback_state IN ('satisfied','dissatisfied')
        GROUP BY callback_state
        """
    )
    d = {r["callback_state"]: r["c"] for r in rows}
    sat, dis = d.get("satisfied", 0), d.get("dissatisfied", 0)
    total = sat + dis
    by_cat = query(
        """
        SELECT category,
               SUM(CASE WHEN callback_state='satisfied' THEN 1 ELSE 0 END) AS sat,
               SUM(CASE WHEN callback_state='dissatisfied' THEN 1 ELSE 0 END) AS dis
        FROM tickets WHERE callback_state IN ('satisfied','dissatisfied')
        GROUP BY category
        """
    )
    reopened = query_one(
        "SELECT COUNT(*) c FROM tickets WHERE state='reopened'")["c"]
    return {"satisfied": sat, "dissatisfied": dis, "total": total,
            "rate": round(sat / total, 3) if total else None,
            "reopened": reopened, "by_category": by_cat}


def make_server(host="127.0.0.1", port=8000):
    return ThreadingHTTPServer((host, port), Handler)
