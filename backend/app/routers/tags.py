"""Словарь тегов и локальный бэкфилл первого шага каскада."""

from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import Session, select

from ..db.models import Tag, Work, WorkTag
from ..db.session import get_session
from ..tagging.meta import genre_labels, tag_from_metadata
from ..tagging.store import apply_meta_tags
from ..tagging.vocab import VOCAB

router = APIRouter(prefix="/api/tags", tags=["tags"])
SessionDep = Annotated[Session, Depends(get_session)]
_ORDER = {
    (group, name): i
    for i, (group, name) in enumerate(
        (group, name) for group, entries in VOCAB.items() for name in entries
    )
}
_GENRE_GROUPS = {"Жанр", "Нон-фикшн", "IT: темы", "IT: языки и технологии"}


@router.get("")
def list_tags(session: SessionDep) -> list[dict]:
    counts = dict(
        session.exec(
            select(WorkTag.tag_id, func.count(WorkTag.work_id))
            .join(Work, Work.id == WorkTag.work_id)
            .where(WorkTag.removed.is_(False), ~Work.hidden.is_(True))
            .group_by(WorkTag.tag_id)
        ).all()
    )
    tags = session.exec(select(Tag).where(Tag.state == "active")).all()
    tags.sort(
        key=lambda t: (_ORDER.get((t.group, t.name), len(_ORDER)), t.group, t.name)
    )
    return [
        {"id": t.id, "name": t.name, "group": t.group, "count": counts.get(t.id, 0)}
        for t in tags
    ]


@router.post("/backfill")
def backfill_tags(session: SessionDep, dry_run: bool = True) -> dict:
    """Статистика кандидатов шага 1; dry_run даже не flush-ит сессию."""
    stats = {
        "books": 0,
        "with_any_tag": 0,
        "with_genre": 0,
        "with_3plus": 0,
        "without_labels": 0,
    }
    statuses = Counter({"": 0, "done": 0, "wip": 0, "dead": 0})
    unknown = Counter()
    for work in session.exec(select(Work).order_by(Work.id)).all():
        result = tag_from_metadata(work)
        stats["books"] += 1
        stats["with_any_tag"] += bool(result.tags)
        stats["with_genre"] += any(g in _GENRE_GROUPS for g, _ in result.tags)
        stats["with_3plus"] += len(result.tags) >= 3
        # Статус — отдельный сигнал, а не тематическая/контентная метка.
        stats["without_labels"] += not (
            genre_labels(work.genres, context=f"Книга {work.id} ({work.title})")
            or (work.rating or "").strip()
            or (work.fandom or "").strip()
        )
        statuses[result.status] += 1
        unknown.update(result.unknown_labels)
        if not dry_run:
            apply_meta_tags(session, work)
    if not dry_run:
        session.commit()
    return {
        **stats,
        "status_counts": dict(statuses),
        "unknown_labels": unknown.most_common(50),
    }
