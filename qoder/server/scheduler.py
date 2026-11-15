"""SM-2 简化调度（SPEC「SM-2 简化规则」逐条对应）。

唯一事实来源是 PowerContext Memory，本模块只做纯计算，不碰任何 IO。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

EASE_MIN = 1.3
EASE_MAX = 2.8
DEFAULT_EASE = 2.5

#: SPEC 同时列出「score<=2 时 ease -= 0.2」与通用 ease 公式，两者按字面叠加（低分双重惩罚）。
FAILURE_EASE_PENALTY = 0.2


def _half_up(value: float) -> int:
    """SPEC 的 round()：用 half-up 避开 Python 内置 round() 的银行家舍入。"""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def initial_card(*, subject: str, topic: str, note: str, today: date) -> dict[str, Any]:
    """新建卡片：interval=0 且 due=今天，因此立刻进入今日复习队列。"""
    return {
        "subject": subject,
        "topic": topic,
        "due": today.isoformat(),
        "ease": DEFAULT_EASE,
        "interval": 0,
        "reps": 0,
        "last": 0,
        "note": note,
    }


def next_review(card: dict[str, Any], score: int, today: date) -> dict[str, Any]:
    """按 SPEC 规则算出自评后的新卡片状态（不修改传入的 card）。"""
    if not 0 <= score <= 5:
        raise ValueError(f"score 必须在 0–5 之间，收到 {score}")

    ease = _clamp(_as_float(card.get("ease"), DEFAULT_EASE), EASE_MIN, EASE_MAX)
    interval = max(0, _as_int(card.get("interval"), 0))
    reps = max(0, _as_int(card.get("reps"), 0))

    if score >= 4:
        reps += 1
        if reps == 1:
            interval = 1
        elif reps == 2:
            interval = 6
        else:
            interval = max(1, _half_up(interval * ease))
    elif score == 3:
        interval = max(1, _half_up(interval * 0.6))
    else:
        reps = 0
        interval = 1
        ease = max(EASE_MIN, ease - FAILURE_EASE_PENALTY)

    ease = _clamp(ease + (0.1 - (5 - score) * 0.08), EASE_MIN, EASE_MAX)
    ease = float(Decimal(str(ease)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    due = today + timedelta(days=interval)
    return {
        "subject": str(card.get("subject") or ""),
        "topic": str(card.get("topic") or ""),
        "due": due.isoformat(),
        "ease": ease,
        "interval": interval,
        "reps": reps,
        "last": score,
        "note": str(card.get("note") or ""),
    }


def is_due(card: dict[str, Any], today: date) -> bool:
    """due 为 ISO 日期；缺失或无法解析时按「到期」处理，宁可多复习不可漏。"""
    raw = str(card.get("due") or "")
    try:
        return date.fromisoformat(raw) <= today
    except ValueError:
        return True
