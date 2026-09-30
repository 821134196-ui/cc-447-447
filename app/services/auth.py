"""用户、角色与数据权限。

角色：
  dispatcher 值班员：提交曲线/指令、复核、观察期结束判断、报修处置
  manager    供热经理：越限指令批准（唯一）、其余值班员权限
  ops        运维：执行指令、关闭现场类事件、断点补录；不能批准指令
  service    客服：仅可见脱敏住户信息与报修，不可见工艺数据
"""
import secrets

from ..db import row_to_dict

# 进程内会话令牌
_TOKENS = {}

ROLE_LABELS = {
    "dispatcher": "值班员",
    "manager": "供热经理",
    "ops": "运维",
    "service": "客服",
}

# 各角色可访问的接口范围（前缀匹配，在 http 层校验）
CAN_READ_PROCESS = {"dispatcher", "manager", "ops"}       # 工艺读数/曲线
CAN_APPROVE = {"manager"}                                 # 越限批准
CAN_REVIEW = {"dispatcher", "manager"}                    # 常规复核
CAN_SUBMIT = {"dispatcher", "manager"}                    # 提交指令
CAN_EXECUTE = {"ops", "manager"}                          # 执行
CAN_BACKFILL = {"ops", "manager"}                         # 断点补录
CAN_HANDOVER = {"dispatcher", "manager"}                  # 班次交接


def login(conn, username):
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        return None, None
    token = secrets.token_hex(16)
    user = row_to_dict(row)
    _TOKENS[token] = user["id"]
    return token, user


def logout(token):
    _TOKENS.pop(token, None)


def user_by_token(conn, token):
    uid = _TOKENS.get(token)
    if uid is None:
        return None
    return row_to_dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def require(user, roles):
    """角色断言；不满足返回 False（HTTP 层转 403）。"""
    return user is not None and user["role"] in roles


def mask_name(name):
    if not name:
        return ""
    if len(name) <= 1:
        return name
    return name[0] + "*" * (len(name) - 1)


def mask_phone(phone):
    if not phone:
        return ""
    if len(phone) >= 7:
        return phone[:3] + "****" + phone[-4:]
    return "****"


def mask_resident(row):
    """客服视角：住户信息脱敏。"""
    d = dict(row)
    d["name"] = mask_name(d.get("name", ""))
    d["phone"] = mask_phone(d.get("phone", ""))
    return d
