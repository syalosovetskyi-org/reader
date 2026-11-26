"""Шаг 1 каскада: только локальные метаданные, без сети и моделей."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache

from ..db.models import Work
from .vocab import DROP, FANDOMS, STATUS_LABELS, VOCAB

log = logging.getLogger("reader.tagging.meta")
TagKey = tuple[str, str]
_FRAMES = {
    '"': '"',
    "'": "'",
    "`": "`",
    "«": "»",
    "“": "”",
    "‘": "’",
    "(": ")",
    "[": "]",
    "{": "}",
}


# Кэш: classify сверяет метку со всеми ~800 синонимами словаря, и без кэша
# прогон по библиотеке нормализовал бы каждый синоним миллионы раз.
@lru_cache(maxsize=8192)
def normalize(s: str) -> str:
    value = " ".join(s.lower().replace("ё", "е").split())
    while len(value) >= 2 and _FRAMES.get(value[0]) == value[-1]:
        value = value[1:-1].strip()
    return value


def _matches(value: str, aliases: list[str], *, substring: bool) -> bool:
    return any(
        normalize(alias[1:]) in value if substring else normalize(alias) == value
        for alias in aliases
        if alias.startswith("~") == substring
    )


@lru_cache(maxsize=8192)
def classify(label: str) -> list[TagKey] | str | None:
    """Все точные совпадения; только при их отсутствии — все подстрочные.

    Результат кэшируется — вызывающий не должен менять возвращённый список.
    """
    value = normalize(label)
    if not label.strip():
        return None
    for substring in (False, True):
        tags = [
            (group, name)
            for group, entries in VOCAB.items()
            for name, aliases in entries.items()
            if _matches(value, [name, *aliases], substring=substring)
        ]
        if tags:
            return tags
        if _matches(value, DROP, substring=substring):
            return "drop"
    return None


def normalize_fandom(label: str) -> str | None:
    """Каноническое имя фандома. Серия Calibre сюда не попадёт."""
    value = normalize(label)
    for substring in (False, True):
        for name, aliases in FANDOMS.items():
            if _matches(value, [name, *aliases], substring=substring):
                return name
    return None


def genre_labels(genres: str, *, context: str = "") -> list[str]:
    """JSON-массив либо CSV; TAGS: бывает и у строки, и у элемента JSON."""
    raw = re.sub(r"^\s*tags:\s*", "", genres or "", flags=re.IGNORECASE)
    if raw.lstrip().startswith("["):
        try:
            labels = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning(
                "%s: разбор genres как JSON, значение %r: %s", context, genres, exc
            )
        else:
            if isinstance(labels, list) and all(isinstance(s, str) for s in labels):
                return [
                    re.sub(r"^\s*tags:\s*", "", s, flags=re.IGNORECASE).strip()
                    for s in labels
                    if s.strip()
                ]
            log.warning(
                "%s: разбор genres: ожидался JSON-список строк, получено %r",
                context,
                genres,
            )
    return [s.strip() for s in raw.split(",") if s.strip()]


def _status(label: str) -> str:
    value = normalize(label)
    return next(
        (
            status
            for status, aliases in STATUS_LABELS.items()
            if _matches(value, aliases, substring=False)
        ),
        "",
    )


@dataclass
class MetadataTags:
    tags: list[TagKey] = field(default_factory=list)
    status: str = ""
    unknown_labels: list[str] = field(default_factory=list)


def tag_from_metadata(work: Work) -> MetadataTags:
    result = MetadataTags()
    labels = genre_labels(work.genres, context=f"Книга {work.id} ({work.title})")
    if work.rating and work.rating.strip():
        labels.append(work.rating.strip())
    status = _status(work.status or "")
    for label in labels:
        label_status = _status(label)
        status = status or label_status  # явный Work.status приоритетнее меток
        classified = classify(label)
        if isinstance(classified, list):
            result.tags.extend(tag for tag in classified if tag not in result.tags)
        elif classified != "drop" and not label_status and not normalize_fandom(label):
            result.unknown_labels.append(label)
    # FANDOMS — отдельный справочник, без группы в VOCAB. Ни фандом, ни серия
    # не становятся тегом; неизвестные значения остаются доступны статистике.
    if work.fandom and work.fandom.strip() and not normalize_fandom(work.fandom):
        result.unknown_labels.append(work.fandom.strip())
    result.status = status if work.site != "pythondocs" else ""
    return result
