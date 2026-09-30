"""供暖目标曲线：折点版本管理 + 乐观锁版本竞争 + 越限判定。

曲线按 (站点, 楼栋) 维护，版本号只增。值班员编辑时必须带 base_version；
两名值班员基于同一版本提交时，后提交者收到 409 冲突提示。
"""
import json

from .. import config
from ..db import row_to_dict
from . import timeutil as t


class ConflictError(Exception):
    """曲线版本冲突（HTTP 层转 409）。"""


class ValidationError(Exception):
    pass


def validate_points(points):
    if not isinstance(points, list) or len(points) < 2:
        raise ValidationError("曲线至少需要 2 个折点")
    last_o = None
    for p in points:
        for k in ("outdoor", "supply", "return"):
            if k not in p:
                raise ValidationError(f"折点缺少字段 {k}")
            if not isinstance(p[k], (int, float)):
                raise ValidationError("折点值必须是数字")
        if p["supply"] <= p["return"]:
            raise ValidationError("供水温度必须高于回水温度")
        if last_o is not None and p["outdoor"] <= last_o:
            raise ValidationError("折点必须按室外温度升序排列")
        last_o = p["outdoor"]
    return True


def check_over_limit(points):
    """返回 (是否越限, 明细列表)。任何折点越过安全区间即越限。"""
    details = []
    for p in points:
        if not (config.SUPPLY_MIN <= p["supply"] <= config.SUPPLY_MAX):
            details.append(f"室外{p['outdoor']}℃ 折点供水 {p['supply']}℃ "
                           f"超出 [{config.SUPPLY_MIN}, {config.SUPPLY_MAX}]")
        if not (config.RETURN_MIN <= p["return"] <= config.RETURN_MAX):
            details.append(f"室外{p['outdoor']}℃ 折点回水 {p['return']}℃ "
                           f"超出 [{config.RETURN_MIN}, {config.RETURN_MAX}]")
        d = p["supply"] - p["return"]
        if not (config.DIFF_MIN <= d <= config.DIFF_MAX):
            details.append(f"室外{p['outdoor']}℃ 折点温差 {d:.1f}℃ "
                           f"超出 [{config.DIFF_MIN}, {config.DIFF_MAX}]")
    return (bool(details), details)


def current_version(conn, station_id, building_id):
    row = conn.execute(
        """SELECT * FROM curve_versions
           WHERE station_id=? AND building_id=?
           ORDER BY version DESC LIMIT 1""",
        (station_id, building_id)).fetchone()
    return row_to_dict(row)


def list_versions(conn, station_id, building_id):
    return [row_to_dict(r) for r in conn.execute(
        """SELECT c.*, u.display_name AS author_name FROM curve_versions c
           JOIN users u ON u.id=c.created_by
           WHERE station_id=? AND building_id=? ORDER BY version DESC""",
        (station_id, building_id)).fetchall()]


def save_curve(conn, station_id, building_id, points, user_id,
               base_version=None, note=""):
    """提交新曲线版本。

    - base_version 为客户端编辑时依据的版本号；
    - 若该 (站点,楼栋) 已有更新版本，抛 ConflictError（后提交者冲突提示）；
    - 返回 (版本行, 越限明细)。越限仍可保存为草稿版本，但指令必须批准。
    """
    validate_points(points)
    cur = current_version(conn, station_id, building_id)
    latest = cur["version"] if cur else 0
    if cur is not None:
        if base_version is None:
            raise ValidationError("必须提供 base_version")
        if int(base_version) != latest:
            other = conn.execute(
                """SELECT c.*, u.display_name FROM curve_versions c
                   JOIN users u ON u.id=c.created_by
                   WHERE station_id=? AND building_id=? AND version=?""",
                (station_id, building_id, latest)).fetchone()
            raise ConflictError(
                f"曲线已被 {other['display_name']} 更新到 v{latest}，"
                f"您基于 v{base_version} 的修改未提交，请刷新后合并修改")
    over, details = check_over_limit(points)
    new_ver = latest + 1
    conn.execute(
        """INSERT INTO curve_versions(station_id, building_id, version,
               points_json, base_version, created_by, created_at, note)
           VALUES(?,?,?,?,?,?,?,?)""",
        (station_id, building_id, new_ver,
         json.dumps(points, ensure_ascii=False),
         base_version, user_id, t.iso_now(), note))
    conn.commit()
    return current_version(conn, station_id, building_id), details


def interpolate(curve_points, outdoor):
    """按室外温度线性插值求当前目标供/回水温度。"""
    ps = sorted(curve_points, key=lambda p: p["outdoor"])
    if outdoor <= ps[0]["outdoor"]:
        return ps[0]["supply"], ps[0]["return"]
    if outdoor >= ps[-1]["outdoor"]:
        return ps[-1]["supply"], ps[-1]["return"]
    for a, b in zip(ps, ps[1:]):
        if a["outdoor"] <= outdoor <= b["outdoor"]:
            f = (outdoor - a["outdoor"]) / (b["outdoor"] - a["outdoor"])
            return (a["supply"] + f * (b["supply"] - a["supply"]),
                    a["return"] + f * (b["return"] - a["return"]))
    return ps[-1]["supply"], ps[-1]["return"]
