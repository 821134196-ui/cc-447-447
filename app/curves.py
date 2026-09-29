"""供暖曲线：版本链、乐观并发、越限判定与插值。"""
from __future__ import annotations

from .db import dumps, now_iso, query, query_one, tx

# 工艺允许区间
SUPPLY_RANGE = (35.0, 85.0)
RETURN_RANGE = (25.0, 65.0)
DIFF_RANGE = (3.0, 30.0)
# 越限阈值（落在区间外即需站长批准）
BREACH_SUPPLY = (40.0, 75.0)
BREACH_DIFF = (5.0, 20.0)
BREACH_MAX_SHIFT = 10.0  # 与生效曲线相比单点最大偏移


class CurveError(ValueError):
    pass


class VersionConflict(CurveError):
    """乐观版本冲突：base_version 已不是最新版本。"""


def normalize_points(points):
    """校验并归一化锚点 [{outdoor,supply,return}, ...]，outdoor 升序。"""
    if not isinstance(points, list) or not (2 <= len(points) <= 8):
        raise CurveError("曲线需要 2~8 个锚点")
    norm = []
    for p in points:
        try:
            o, s, r = float(p["outdoor"]), float(p["supply"]), float(p["return"])
        except (KeyError, TypeError, ValueError):
            raise CurveError("锚点必须包含 outdoor/supply/return 三个数值")
        if not (-30 <= o <= 15):
            raise CurveError("室外温度锚点需在 -30~15℃ 之间")
        norm.append({"outdoor": round(o, 2), "supply": round(s, 2), "return": round(r, 2)})
    norm.sort(key=lambda x: x["outdoor"])
    for a, b in zip(norm, norm[1:]):
        if b["outdoor"] <= a["outdoor"]:
            raise CurveError("室外温度锚点不能重复")
        # 室外越低，供水越高
        if b["supply"] >= a["supply"]:
            raise CurveError("供水温度必须随室外温度升高而单调降低")
    for p in norm:
        lo, hi = SUPPLY_RANGE
        if not (lo <= p["supply"] <= hi):
            raise CurveError(f"供水温度 {p['supply']} 超出工艺允许区间 {lo}~{hi}℃")
        lo, hi = RETURN_RANGE
        if not (lo <= p["return"] <= hi):
            raise CurveError(f"回水温度 {p['return']} 超出工艺允许区间 {lo}~{hi}℃")
        d = p["supply"] - p["return"]
        lo, hi = DIFF_RANGE
        if not (lo <= d <= hi):
            raise CurveError(f"供回水温差 {d:.1f} 超出允许范围 {lo}~{hi}℃")
    return norm


def breach_reasons(new_points, old_points=None) -> list[str]:
    reasons: list[str] = []
    for p in new_points:
        if p["supply"] > BREACH_SUPPLY[1]:
            reasons.append(f"供水 {p['supply']}℃ > {BREACH_SUPPLY[1]}℃ 高限")
        if p["supply"] < BREACH_SUPPLY[0]:
            reasons.append(f"供水 {p['supply']}℃ < {BREACH_SUPPLY[0]}℃ 低限")
        d = p["supply"] - p["return"]
        if d > BREACH_DIFF[1]:
            reasons.append(f"温差 {d:.1f}℃ > {BREACH_DIFF[1]}℃")
        if d < BREACH_DIFF[0]:
            reasons.append(f"温差 {d:.1f}℃ < {BREACH_DIFF[0]}℃")
    if old_points:
        old_at = {p["outdoor"]: p for p in old_points}
        for p in new_points:
            # 在相同室外锚点或插值位置比较
            s_old = interpolate(old_points, p["outdoor"], "supply")
            if abs(p["supply"] - s_old) > BREACH_MAX_SHIFT:
                reasons.append(
                    f"室外 {p['outdoor']}℃ 处供水调整 {p['supply'] - s_old:+.1f}℃，"
                    f"超过 ±{BREACH_MAX_SHIFT:g}℃ 调整上限"
                )
    # 去重保序
    seen, uniq = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def interpolate(points, outdoor: float, key: str = "supply") -> float:
    xs = [p["outdoor"] for p in points]
    ys = [p[key] for p in points]
    if outdoor <= xs[0]:
        return ys[0]
    if outdoor >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= outdoor <= xs[i + 1]:
            t = (outdoor - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + t * (ys[i + 1] - ys[i])
    return ys[-1]


def active_version(building_id: int):
    return query_one(
        "SELECT * FROM curve_versions WHERE building_id=? AND status='active' "
        "ORDER BY version_no DESC LIMIT 1",
        (building_id,),
    )


def list_versions(building_id: int):
    return query(
        "SELECT v.*, u.display_name AS creator_name FROM curve_versions v "
        "JOIN users u ON u.id=v.created_by WHERE building_id=? ORDER BY version_no DESC",
        (building_id,),
    )


def _get(building_id: int, version_id: int):
    return query_one(
        "SELECT * FROM curve_versions WHERE id=? AND building_id=?",
        (version_id, building_id),
    )


def submit_version(building_id: int, points, user, base_version, note="",
                   source="manual"):
    """创建新曲线版本。base_version 为调用方看到的最新版本号；过期则冲突。"""
    points = normalize_points(points)
    import json
    with tx() as conn:
        head = conn.execute(
            "SELECT * FROM curve_versions WHERE building_id=? "
            "ORDER BY version_no DESC LIMIT 1",
            (building_id,),
        ).fetchone()
        head_no = head["version_no"] if head else 0
        if head and base_version is not None and int(base_version) != head_no:
            raise VersionConflict(
                f"版本冲突：您基于 v{base_version} 编辑，当前最新已是 v{head_no}，"
                "请刷新后基于最新版本重新提交"
            )
        old_pts = json.loads(head["points_json"]) if head else None
        reasons = breach_reasons(points, old_pts)
        row = conn.execute(
            "INSERT INTO curve_versions(building_id, version_no, points_json, source, "
            "base_version, created_by, created_at, status, note) "
            "VALUES(?,?,?,?,?,?,?,'proposed',?) RETURNING *",
            (building_id, head_no + 1, dumps(points), source,
             head_no if head else None, user["id"], now_iso(), note),
        ).fetchone()
        return dict(row), reasons
