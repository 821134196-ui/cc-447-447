"""HTTP 服务：JSON API + 静态页面。仅依赖标准库。

认证：登录后下发 token，客户端以 Cookie hss_token 或 Authorization: Bearer 携带。
授权：按 services.auth 中的角色集合在每个接口显式校验，越权返回 403。
"""
import base64
import json
import os
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config
from .db import get_conn
from .services import auth, curves as curve_svc, commands as cmd_svc
from .services import complaints as cmp_svc, events as event_svc
from .services import readings as reading_svc, shifts as shift_svc
from .services import snapshots as snap_svc
from .services import timeutil as t

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")

STATUS_LABELS = {
    "pending_review": "待复核", "pending_execution": "待执行",
    "observing": "生效观察", "finished": "结束", "rejected": "已驳回",
}


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "HeatingStation/1.0"

    # ---- 基础工具 ---------------------------------------------------------
    def _conn(self):
        return get_conn()

    def _user(self, conn):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = None
        if "hss_token" in cookie:
            token = cookie["hss_token"].value
        else:
            au = self.headers.get("Authorization", "")
            if au.startswith("Bearer "):
                token = au[7:]
        return auth.user_by_token(conn, token) if token else None

    def _require(self, conn, roles):
        user = self._user(conn)
        if not user:
            raise ApiError(401, "未登录")
        if roles is not None and not auth.require(user, roles):
            raise ApiError(403, f"角色「{auth.ROLE_LABELS[user['role']]}」无权操作")
        return user

    def _qs(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise ApiError(400, "请求体不是合法 JSON")

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path):
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # 静默；演示时可打开

    # ---- 路由 -------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        path = urlparse(self.path).path
        conn = self._conn()
        try:
            if path.startswith("/api/"):
                self._route_api(conn, method, path)
            else:
                self._serve_static(path)
        except ApiError as e:
            self._send_json({"error": e.message}, e.status)
        except PermissionError as e:
            self._send_json({"error": str(e)}, 403)
        except (curve_svc.ConflictError,) as e:
            self._send_json({"error": str(e), "code": "curve_conflict"}, 409)
        except (curve_svc.ValidationError, cmd_svc.CommandError, ValueError) as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"服务器错误: {e}"}, 500)
        finally:
            conn.close()

    def _serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        # 防目录穿越
        rel = os.path.normpath(path.lstrip("/"))
        full = os.path.join(WEB_DIR, rel)
        if not full.startswith(WEB_DIR) or os.path.isdir(full):
            if path == "/index.html":
                full = os.path.join(WEB_DIR, "index.html")
            else:
                self.send_error(404)
                return
        self._send_file(full)

    # ---- API ---------------------------------------------------------------
    def _route_api(self, conn, method, path):
        q = self._qs()
        p = path.split("/")
        # p: ['', 'api', ...]

        if path == "/api/login" and method == "POST":
            body = self._body()
            token, user = auth.login(conn, body.get("username", ""))
            if not user:
                raise ApiError(401, "用户不存在")
            data = json.dumps(
                {"token": token, "user": self._public_user(user)},
                ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header(
                "Set-Cookie",
                f"hss_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/logout" and method == "POST":
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if "hss_token" in cookie:
                auth.logout(cookie["hss_token"].value)
            self.send_response(200)
            self.send_header(
                "Set-Cookie", "hss_token=; Path=/; Max-Age=0")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if path == "/api/me":
            self._send_json({"user": self._public_user(self._require(conn, None))})
            return

        if path == "/api/stations":
            self._require(conn, None)
            self._send_json({"stations": self._stations(conn)})
            return

        if path == "/api/overview":
            user = self._require(conn, None)
            self._send_json(self._overview(conn, user))
            return

        if path == "/api/readings":
            self._require(conn, auth.CAN_READ_PROCESS)
            self._send_json({"readings": self._readings(conn, q)})
            return

        if path == "/api/trend":
            self._require(conn, auth.CAN_READ_PROCESS)
            ids = [int(x) for x in q.get("buildings", "").split(",") if x]
            hours = int(q.get("hours", "12"))
            self._send_json({"series": self._trend(conn, ids, hours)})
            return

        if path == "/api/readings/backfill" and method == "POST":
            user = self._require(conn, auth.CAN_BACKFILL)
            self._backfill(conn, user)
            return

        if path == "/api/curve":
            if method == "GET":
                self._require(conn, auth.CAN_READ_PROCESS)
                self._send_json(self._get_curve(conn, q))
            else:
                user = self._require(conn, auth.CAN_SUBMIT)
                self._save_curve(conn, user)
            return

        if path == "/api/commands":
            if method == "GET":
                self._require(conn, auth.CAN_READ_PROCESS)
                self._send_json({"commands": cmd_svc.list_(
                    conn, q.get("status"), q.get("building_id"))})
            else:
                user = self._require(conn, auth.CAN_SUBMIT)
                body = self._body()
                cmd = cmd_svc.submit(conn, int(body["station_id"]),
                                     int(body["building_id"]), user["id"],
                                     body.get("note", ""))
                self._send_json({"command": cmd}, 201)
            return

        if path.startswith("/api/commands/") and p[-1] == "curves":
            self._require(conn, auth.CAN_READ_PROCESS)
            cid = int(p[3])
            self._send_json(self._command_curves(conn, cid))
            return
        if path.startswith("/api/commands/") and len(p) == 4:
            cid = int(p[3])
            if method == "GET":
                self._require(conn, auth.CAN_READ_PROCESS)
                cmd = cmd_svc.get(conn, cid)
                if not cmd:
                    raise ApiError(404, "指令不存在")
                cmd["status_label"] = STATUS_LABELS.get(cmd["status"], cmd["status"])
                cmd["linked_complaints"] = cmd_svc.linked_complaints(conn, cid)
                self._send_json({"command": cmd})
                return
            return
        if path.startswith("/api/commands/") and method == "POST":
            cid = int(p[3])
            action = p[4]
            body = self._body()
            if action == "review":
                user = self._require(conn, auth.CAN_REVIEW | {"manager"})
                cmd = cmd_svc.review(conn, cid, user,
                                     bool(body.get("approve")), body.get("note", ""))
            elif action == "execute":
                user = self._require(conn, auth.CAN_EXECUTE)
                cmd = cmd_svc.execute(conn, cid, user)
            elif action == "finish":
                user = self._require(conn, auth.CAN_REVIEW)
                cmd = cmd_svc.finish(conn, cid, user, body.get("effect_note", ""))
            else:
                raise ApiError(404, "未知操作")
            self._send_json({"command": cmd})
            return

        if path == "/api/complaints":
            user = self._require(conn, None)
            if method == "GET":
                items = cmp_svc.list_complaints(conn, q.get("station_id"))
                self._send_json({"complaints": self._mask_complaints(items, user)})
            else:
                # 客服/值班员登记报修
                self._require(conn, {"service", "dispatcher", "manager"})
                body = self._body()
                row, merged, mass = cmp_svc.create_complaint(
                    conn, int(body["station_id"]), int(body["building_id"]),
                    body.get("kind", "other"), body.get("content", ""),
                    body.get("resident_id"), body.get("source", "phone"))
                self._send_json({"complaint": row, "merged_into": merged,
                                 "mass_cold": mass}, 201)
            return
        if path.startswith("/api/complaints/") and method == "POST":
            cid = int(p[3])
            action = p[4]
            body = self._body()
            if action == "resolve":
                user = self._require(conn, {"ops", "dispatcher", "manager"})
                row = cmp_svc.resolve(conn, cid, user, body.get("note", ""))
                self._send_json({"complaint": row})
            elif action == "callback":
                user = self._require(conn, {"service", "dispatcher", "manager"})
                row = cmp_svc.add_callback(
                    conn, cid, body.get("result", "satisfied"),
                    body.get("note", ""), user)
                self._send_json({"callback": row})
            else:
                raise ApiError(404, "未知操作")
            return
        if path == "/api/density":
            self._require(conn, None)
            self._send_json({"density": cmp_svc.density_by_building(
                conn, int(q.get("station_id", "1")))})
            return
        if path == "/api/callback-stats":
            self._require(conn, None)
            self._send_json(cmp_svc.callback_stats(
                conn, int(q.get("station_id", "1")), int(q.get("days", "7"))))
            return

        if path == "/api/events":
            user = self._require(conn, None)
            kind = q.get("kind")
            # 客服仅可见两类服务事件，且无权查看工艺事件
            if user["role"] == "service":
                allowed = ("callback_unsatisfied", "repeat_repair")
                if kind and kind not in allowed:
                    raise ApiError(403, "客服无权查看工艺事件")
                kind = kind or allowed[0]
                rows = event_svc.list_events(conn, q.get("status"), kind,
                                             q.get("station_id"))
                rows += event_svc.list_events(conn, q.get("status"), allowed[1],
                                              q.get("station_id"))
                self._send_json({"events": rows})
            else:
                self._send_json({"events": event_svc.list_events(
                    conn, q.get("status"), kind, q.get("station_id"))})
            return
        if path.startswith("/api/events/") and method == "POST" and p[4] == "close":
            user = self._require(conn, {"ops", "dispatcher", "manager"})
            body = self._body()
            row = event_svc.close_event(conn, int(p[3]), user,
                                        body.get("note", ""))
            self._send_json({"event": row})
            return

        if path == "/api/shifts":
            self._require(conn, {"dispatcher", "manager", "ops"})
            sid = int(q.get("station_id", "1"))
            active = shift_svc.active_shift(conn, sid)
            self._send_json({
                "active": active,
                "shifts": shift_svc.list_shifts(conn, sid)})
            return
        if path == "/api/shifts/open" and method == "POST":
            user = self._require(conn, {"dispatcher", "manager"})
            body = self._body()
            row = shift_svc.open_shift(conn, int(body["station_id"]),
                                       body["leader"], body.get("note", ""))
            self._send_json({"shift": row})
            return
        if path.startswith("/api/shifts/") and p[4] == "snapshot" and method == "POST":
            user = self._require(conn, {"dispatcher", "manager"})
            row = snap_svc.take_snapshot(conn, int(p[3]))
            self._send_json({"snapshot": row})
            return
        if path == "/api/snapshots":
            self._require(conn, {"dispatcher", "manager", "ops"})
            self._send_json({"snapshots": snap_svc.list_snapshots(
                conn, q.get("shift_id"))})
            return
        if path == "/api/handovers":
            self._require(conn, {"dispatcher", "manager", "ops"})
            if method == "GET":
                self._send_json({"handovers": shift_svc.list_handovers(
                    conn, int(q.get("station_id", "1")))})
            else:
                self._handover(conn)
            return
        if path.startswith("/api/handovers/") and p[-1] == "attachment":
            self._require(conn, {"dispatcher", "manager", "ops"})
            hid = int(p[3])
            row = conn.execute("SELECT * FROM handovers WHERE id=?", (hid,)).fetchone()
            if not row:
                raise ApiError(404, "交接记录不存在")
            data = shift_svc.read_attachment(row["attachment_path"])
            if data is None:
                raise ApiError(404, "附件不存在")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{row["attachment_name"]}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/notifications":
            self._require(conn, None)
            rows = conn.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT 50").fetchall()
            self._send_json({"notifications": [dict(r) for r in rows]})
            return
        if path == "/api/sim/inject" and method == "POST":
            user = self._require(conn, {"dispatcher", "manager"})
            body = self._body()
            self.server.sim.inject(int(body["building_id"]),
                                   body.get("mode", "cold"))
            self._send_json({"ok": True})
            return
        if path == "/api/health":
            self._send_json({"ok": True, "time": t.iso_now()})
            return

        raise ApiError(404, f"接口不存在: {path}")

    # ---- 数据组装 -----------------------------------------------------------
    def _public_user(self, u):
        return {"id": u["id"], "username": u["username"],
                "display_name": u["display_name"], "role": u["role"],
                "role_label": auth.ROLE_LABELS[u["role"]]}

    def _stations(self, conn):
        out = []
        for s in conn.execute("SELECT * FROM stations").fetchall():
            d = dict(s)
            d["buildings"] = [dict(r) for r in conn.execute(
                "SELECT * FROM buildings WHERE station_id=? ORDER BY id",
                (s["id"],)).fetchall()]
            out.append(d)
        return out

    def _overview(self, conn, user):
        sid = 1
        buildings = [dict(r) for r in conn.execute(
            "SELECT * FROM buildings WHERE station_id=? ORDER BY id", (sid,)).fetchall()]
        is_service = user["role"] == "service"
        latest = {}
        if not is_service:
            latest_rows = conn.execute(
                """SELECT r.* FROM readings r
                   JOIN (SELECT building_id, MAX(ts) mts FROM readings GROUP BY building_id) x
                     ON r.building_id=x.building_id AND r.ts=x.mts""").fetchall()
            latest = {r["building_id"]: dict(r) for r in latest_rows}
        # 客服仅关注服务类事件；其余角色可见全部七类
        event_filter = (" AND kind IN ('callback_unsatisfied','repeat_repair')"
                        if is_service else "")
        open_events = conn.execute(
            "SELECT kind, COUNT(*) n FROM events WHERE status='open'"
            + event_filter + " GROUP BY kind").fetchall()
        cmds = [] if is_service else conn.execute(
            "SELECT status, COUNT(*) n FROM commands "
            "WHERE status IN ('pending_review','pending_execution','observing') "
            "GROUP BY status").fetchall()
        active = None if is_service else shift_svc.active_shift(conn, sid)
        chain_ok, snap_count = (True, 0)
        if not is_service:
            chain_ok, _ = snap_svc.verify_chain(conn)
            snap_count = conn.execute("SELECT COUNT(*) n FROM snapshots").fetchone()["n"]
        out = {
            "station": dict(conn.execute(
                "SELECT * FROM stations WHERE id=?", (sid,)).fetchone()),
            "buildings": buildings,
            "latest": latest,
            "open_events": {r["kind"]: r["n"] for r in open_events},
            "commands": {r["status"]: r["n"] for r in cmds},
            "open_complaints": conn.execute(
                "SELECT COUNT(*) n FROM complaints WHERE status='open'").fetchone()["n"],
            "active_shift": active,
            "snapshot_count": snap_count,
            "chain_ok": chain_ok,
            "role": user["role"],
        }
        return out

    def _readings(self, conn, q):
        bid = q.get("building_id")
        hours = int(q.get("hours", "12"))
        w0 = t.hours_ago(hours)
        sql = "SELECT * FROM readings WHERE ts>=?"
        args = [w0]
        if bid:
            sql += " AND building_id=?"
            args.append(bid)
        sql += " ORDER BY ts, id"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def _trend(self, conn, ids, hours):
        w0 = t.hours_ago(hours)
        out = {}
        if not ids:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM buildings ORDER BY id").fetchall()]
        for bid in ids:
            rows = conn.execute(
                """SELECT ts, supply_temp, return_temp, outdoor_temp,
                          supply_pressure FROM readings
                   WHERE building_id=? AND ts>=? ORDER BY ts""",
                (bid, w0)).fetchall()
            name = conn.execute("SELECT name FROM buildings WHERE id=?",
                                (bid,)).fetchone()["name"]
            out[str(bid)] = {"name": name,
                             "points": [dict(r) for r in rows]}
        return out

    def _backfill(self, conn, user):
        body = self._body()
        rid, closed = reading_svc.backfill_reading(
            conn, int(body["station_id"]), int(body["building_id"]),
            ts=body["ts"],
            outdoor_temp=float(body["outdoor_temp"]),
            supply_temp=float(body["supply_temp"]),
            return_temp=float(body["return_temp"]),
            supply_pressure=float(body["supply_pressure"]),
            return_pressure=float(body["return_pressure"]),
            pump_status=body.get("pump_status", "running"),
            pump_frequency=float(body.get("pump_frequency", 38.0)))
        self._send_json({"reading_id": rid, "closed_events": closed})

    def _get_curve(self, conn, q):
        bid = int(q["building_id"])
        cur = curve_svc.current_version(conn, int(q.get("station_id", "1")), bid)
        versions = curve_svc.list_versions(conn, int(q.get("station_id", "1")), bid)
        return {"current": cur, "versions": versions}

    def _save_curve(self, conn, user):
        body = self._body()
        ver, over_details = curve_svc.save_curve(
            conn, int(body["station_id"]), int(body["building_id"]),
            body["points"], user["id"], body.get("base_version"),
            body.get("note", ""))
        self._send_json({"curve": ver, "over_limit_details": over_details}, 201)

    def _command_curves(self, conn, cid):
        cmd = cmd_svc.get(conn, cid)
        if not cmd:
            raise ApiError(404, "指令不存在")
        old = new = None
        if cmd["old_curve_version"]:
            r = conn.execute(
                "SELECT * FROM curve_versions WHERE building_id=? AND version=?",
                (cmd["building_id"], cmd["old_curve_version"])).fetchone()
            old = _ver(r)
        r = conn.execute(
            "SELECT * FROM curve_versions WHERE building_id=? AND version=?",
            (cmd["building_id"], cmd["new_curve_version"])).fetchone()
        new = _ver(r)
        linked = cmd_svc.linked_complaints(conn, cid)
        return {"command": cmd, "old": old, "new": new,
                "linked_complaints": linked}

    def _mask_complaints(self, items, user):
        if user["role"] != "service":
            return items
        for it in items:
            if it.get("resident_name"):
                it["resident_name"] = auth.mask_name(it["resident_name"])
            if it.get("resident_phone"):
                it["resident_phone"] = auth.mask_phone(it["resident_phone"])
            if it.get("room"):
                it["room"] = it["room"][0] + "***"
        return items

    def _handover(self, conn):
        user = self._require(conn, auth.CAN_HANDOVER)
        body = self._body()
        raw = base64.b64decode(body["attachment_base64"]) if body.get(
            "attachment_base64") else None
        row = shift_svc.create_handover(
            conn, int(body["shift_id"]), int(body["station_id"]), user,
            body["incoming_leader"], body["summary"],
            body.get("attachment_name"), raw)
        self._send_json({"handover": row}, 201)


def _ver(row):
    if not row:
        return None
    d = dict(row)
    d["points"] = json.loads(d.pop("points_json"))
    return d


def make_server(host=None, port=None, sim=None):
    httpd = ThreadingHTTPServer(
        (config.HOST if host is None else host,
         config.PORT if port is None else port), Handler)
    httpd.sim = sim
    return httpd
