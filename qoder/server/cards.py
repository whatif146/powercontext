"""知识点卡片的编解码 + 笔记切分（SPEC「卡片文本格式」）。

一行卡片 = 一条 memory entry，形如：
  card|subject=数据结构|topic=红黑树插入|due=2026-11-16|ease=2.5|interval=3|reps=2|last=4|note=<原句摘要>
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

KIND_CARD = "review_card"
KIND_LOG = "review_log"
KIND_PREFERENCE = "preference"

CARD_PREFIX = "card|"
LOG_PREFIX = "log|"
PREF_PREFIX = "pref|"

#: note 不能包含 | 与换行，超长截断到 120 字（SPEC）
NOTE_MAX_CHARS = 120
TOPIC_MAX_CHARS = 40
SUBJECT_MAX_CHARS = 40

#: 一次粘贴最多切多少张卡，避免整本书粘贴把 scope 灌满
MAX_CARDS_PER_BATCH = 40
#: 过短的碎片（如「小结」「。」）不值得单独成卡
MIN_TOPIC_CHARS = 2

_CONTROL_RE = re.compile(r"[\r\n\t\|\x00-\x1f\x7f]+")
_WHITESPACE_RE = re.compile(r"\s+")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_BULLET_RE = re.compile(r"^\s*(?:[-*+•·‣◦>]+\s*|\[[ xX]\]\s*)?")
_NUMBERED_RE = re.compile(r"^\s*(?:[(（]?\d{1,3}[)）.、:：]|[一二三四五六七八九十]{1,3}[、.．:：])\s*")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s*")
_TABLE_SEP_RE = re.compile(r"^\s*[\|:\- ]+$")
_SENTENCE_SPLIT_RE = re.compile(r"[。；;！!？?]+")
_CLAUSE_SPLIT_RE = re.compile(r"[，,、]+")
#: 「术语：解释」形态里充当分隔符的字符
_LABEL_SEPARATORS = ("：", ":", " → ", "→", " - ", " — ")


def sanitize(value: str, *, limit: int) -> str:
    """去掉 | 与换行等控制符，压缩空白，按字数截断。"""
    cleaned = _CONTROL_RE.sub(" ", str(value))
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned[:limit]


def _single_field(value: str, limit: int) -> str:
    return sanitize(value, limit=limit)


def format_card(card: dict[str, Any]) -> str:
    """卡片字典 -> memory text。"""
    ease = card.get("ease", 2.5)
    try:
        ease_text = format(float(ease), "g")
    except (TypeError, ValueError):
        ease_text = "2.5"
    fields = [
        "card",
        f"subject={_single_field(card.get('subject', ''), SUBJECT_MAX_CHARS)}",
        f"topic={_single_field(card.get('topic', ''), TOPIC_MAX_CHARS)}",
        f"due={_single_field(card.get('due', ''), 10)}",
        f"ease={ease_text}",
        f"interval={_int_or(card.get('interval'), 0)}",
        f"reps={_int_or(card.get('reps'), 0)}",
        f"last={_int_or(card.get('last'), 0)}",
        f"note={_single_field(card.get('note', ''), NOTE_MAX_CHARS)}",
    ]
    return "|".join(fields)


def _int_or(value: Any, fallback: int) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return fallback


def parse_card(text: str) -> dict[str, Any] | None:
    """memory text -> 卡片字典。不是卡片行或关键字段缺失时返回 None（调用方负责显式报错）。"""
    if not isinstance(text, str) or not text.startswith(CARD_PREFIX):
        return None

    values: dict[str, str] = {}
    for token in text.split("|")[1:]:
        key, sep, value = token.partition("=")
        if sep:
            values[key.strip()] = value

    topic = values.get("topic", "").strip()
    due = values.get("due", "").strip()
    if not topic or not due:
        return None

    try:
        ease = float(values.get("ease", "2.5"))
    except ValueError:
        ease = 2.5

    return {
        "subject": values.get("subject", "").strip(),
        "topic": topic,
        "due": due,
        "ease": ease,
        "interval": _int_or(values.get("interval"), 0),
        "reps": _int_or(values.get("reps"), 0),
        "last": _int_or(values.get("last"), 0),
        "note": values.get("note", "").strip(),
    }


def format_log(
    *,
    subject: str,
    topic: str,
    entry_id: str,
    score: int,
    interval: int,
    due: str,
    at: str,
) -> str:
    """一次自评记录，同样写成单行、可被 memory/search 召回。

    at 让每次复习都是独立事件；同一秒内的重复提交会被上游按内容去重，正好挡住双击重复计分。
    """
    fields = [
        "log",
        f"subject={_single_field(subject, SUBJECT_MAX_CHARS)}",
        f"topic={_single_field(topic, TOPIC_MAX_CHARS)}",
        f"entry={_single_field(entry_id, 128)}",
        f"score={score}",
        f"interval={_int_or(interval, 0)}",
        f"due={_single_field(due, 10)}",
        f"at={_single_field(at, 32)}",
    ]
    return "|".join(fields)


def parse_log(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str) or not text.startswith(LOG_PREFIX):
        return None
    values: dict[str, str] = {}
    for token in text.split("|")[1:]:
        key, sep, value = token.partition("=")
        if sep:
            values[key.strip()] = value
    return {
        "subject": values.get("subject", "").strip(),
        "topic": values.get("topic", "").strip(),
        "entry": values.get("entry", "").strip(),
        "score": _int_or(values.get("score"), -1),
        "interval": _int_or(values.get("interval"), 0),
        "due": values.get("due", "").strip(),
        "at": values.get("at", "").strip(),
    }


def format_preference(*, max_cards_per_day: int) -> str:
    return f"pref|max_cards_per_day={_int_or(max_cards_per_day, 20)}"


def parse_preference(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str) or not text.startswith(PREF_PREFIX):
        return None
    values: dict[str, str] = {}
    for token in text.split("|")[1:]:
        key, sep, value = token.partition("=")
        if sep:
            values[key.strip()] = value
    if "max_cards_per_day" not in values:
        return None
    return {"max_cards_per_day": _int_or(values["max_cards_per_day"], 20)}


# --------------------------------------------------------------------------- 笔记切分


def split_notes(text: str, *, subject: str, today: date) -> list[dict[str, Any]]:
    """把粘贴的笔记切成待入库卡片（调度字段用 initial_card 的初始值）。"""
    from . import scheduler

    sentences = extract_sentences(text)
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sentence in sentences:
        key = sentence.casefold()
        if key in seen:
            continue
        seen.add(key)
        topic, note = _topic_and_note(sentence)
        cards.append(
            scheduler.initial_card(subject=subject, topic=topic, note=note, today=today)
        )
        if len(cards) >= MAX_CARDS_PER_BATCH:
            break
    return cards


def extract_sentences(text: str) -> list[str]:
    """按行去噪（列表符/标题/编号/代码围栏），再按句末标点切成知识点。"""
    if not isinstance(text, str):
        return []

    out: list[str] = []
    inside_fence = False
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _FENCE_RE.match(raw_line):
            inside_fence = not inside_fence
            continue
        if inside_fence or not raw_line.strip() or _TABLE_SEP_RE.match(raw_line):
            continue
        line = _HEADING_RE.sub("", raw_line)
        line = _BULLET_RE.sub("", line)
        line = _NUMBERED_RE.sub("", line)
        line = sanitize(line, limit=NOTE_MAX_CHARS * 2)
        if not line:
            continue
        for piece in _SENTENCE_SPLIT_RE.split(line):
            piece = sanitize(piece, limit=NOTE_MAX_CHARS * 2)
            if len(piece) >= MIN_TOPIC_CHARS:
                out.append(piece)
    return out


def _topic_and_note(sentence: str) -> tuple[str, str]:
    """「术语：解释」取术语为 topic；否则取首个短句为 topic。整句作为 note 摘要。"""
    for separator in _LABEL_SEPARATORS:
        if separator not in sentence:
            continue
        head, _, tail = sentence.partition(separator)
        head = sanitize(head, limit=TOPIC_MAX_CHARS)
        tail = sanitize(tail, limit=NOTE_MAX_CHARS)
        #: 排除 https: 这类伪分隔，以及「→/-」两侧为空的排版噪声
        if tail.startswith("//") or not tail:
            continue
        if MIN_TOPIC_CHARS <= len(head) <= 30 and len(tail) >= MIN_TOPIC_CHARS:
            return head, sanitize(sentence, limit=NOTE_MAX_CHARS)

    first_clause = _CLAUSE_SPLIT_RE.split(sentence, maxsplit=1)[0].strip()
    if MIN_TOPIC_CHARS <= len(first_clause) <= TOPIC_MAX_CHARS:
        return sanitize(first_clause, limit=TOPIC_MAX_CHARS), sanitize(sentence, limit=NOTE_MAX_CHARS)

    topic = sanitize(sentence, limit=TOPIC_MAX_CHARS)
    if len(sentence) > TOPIC_MAX_CHARS:
        topic = topic[: TOPIC_MAX_CHARS - 1] + "…"
    return topic, sanitize(sentence, limit=NOTE_MAX_CHARS)
