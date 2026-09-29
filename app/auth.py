"""角色、会话与数据权限。

角色：
  dispatcher 值班员 —— 提交曲线/指令、处置事件、交接班
  manager    站长   —— 复核/批准越限指令
  ops        运维   —— 执行指令、断点补录，不能批准
  service    客服   —— 报修受理与回访，仅见脱敏住户信息
"""
from __future__ import annotations

import secrets
import threading

from .db import query_one

ROLES = ("dispatcher", "manager", "ops", "service")
ROLE_NAMES = {
    "dispatcher": "值班员",
    "manager": "站长",
    "ops": "运维",
    "service": "客服",
}

_sessions: dict[str, int] = {}
_lock = threading.Lock()


def login(username: str):
    user = query_one("SELECT * FROM users WHERE username=?", (username,))
    if not user:
        return None
    token = secrets.token_hex(16)
    with _lock:
        _sessions[token] = user["id"]
    return token, dict(user)


def logout(token: str):
    with _lock:
        _sessions.pop(token, None)


def current_user(headers) -> dict | None:
    auth = headers.get("Authorization", "") if headers else ""
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    uid = _sessions.get(token)
    if not uid:
        return None
    return query_one("SELECT * FROM users WHERE id=?", (uid,))


def require(user, *roles):
    if not user:
        raise PermissionError("未登录")
    if roles and user["role"] not in roles:
        raise PermissionError(f"当前角色「{ROLE_NAMES.get(user['role'])}」无权执行该操作")
    return user


# ---- 脱敏 ----
def mask_name(name: str) -> str:
    if not name:
        return ""
    return name[0] + "**"


def mask_phone(phone: str) -> str:
    if not phone or len(phone) < 7:
        return "***"
    return phone[:3] + "****" + phone[-4:]


# 客服与运维只见脱敏信息；值班员/站长可见完整信息
def can_see_resident_detail(user) -> bool:
    return bool(user) and user["role"] in ("dispatcher", "manager")


def mask_resident(row: dict, user) -> dict:
    """对联系人字段就地脱敏（不影响房号，调度需要定位）。"""
    if not row or can_see_resident_detail(user):
        return row
    row = dict(row)
    if row.get("resident_name"):
        row["resident_name"] = mask_name(row["resident_name"])
    if row.get("resident_phone"):
        row["resident_phone"] = mask_phone(row["resident_phone"])
    return row
