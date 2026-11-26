"""Словарь, очередь предложений и управление каскадом разметки."""

import json
from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from ..db.models import Tag, Work, WorkTag
from ..db.session import get_session
from ..tagging import desc
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


def _proposal(session: Session, tag_id: int) -> Tag:
    tag = session.get(Tag, tag_id)
    if tag is None or tag.state != "proposed":
        raise HTTPException(404, "proposal not found")
    return tag


@router.get("/proposals")
def list_proposals(session: SessionDep) -> list[dict]:
    tags = session.exec(select(Tag).where(Tag.state == "proposed")).all()
    books: dict[int, list[dict]] = {}
    rows = session.exec(
        select(WorkTag.tag_id, Work.id, Work.title)
        .join(Work, Work.id == WorkTag.work_id)
        .join(Tag, Tag.id == WorkTag.tag_id)
        .where(Tag.state == "proposed", ~WorkTag.removed, ~Work.hidden.is_(True))
        .order_by(Work.id)
    ).all()
    counts = Counter()
    for tag_id, work_id, title in rows:
        counts[tag_id] += 1
        sample = books.setdefault(tag_id, [])
        if len(sample) < 5:
            sample.append({"id": work_id, "title": title})
    tags.sort(key=lambda tag: (-counts[tag.id], tag.id))
    return [
        {
            "id": tag.id,
            "name": tag.name,
            "group": tag.group,
            "count": counts[tag.id],
            "books": books.get(tag.id, []),
        }
        for tag in tags
    ]


@router.post("/proposals/{tag_id}/approve")
def approve_proposal(tag_id: int, session: SessionDep) -> dict:
    tag = _proposal(session, tag_id)
    tag.state = "active"
    session.add(tag)
    session.commit()
    entry = next(item for item in list_tags(session) if item["id"] == tag_id)
    return {"ok": True, "tag": entry}


class MergeTagIn(BaseModel):
    into_tag_id: int


@router.post("/proposals/{tag_id}/merge")
def merge_proposal(tag_id: int, body: MergeTagIn, session: SessionDep) -> dict:
    proposal = _proposal(session, tag_id)
    target = session.get(Tag, body.into_tag_id)
    if target is None or target.state != "active":
        raise HTTPException(404, "active target tag not found")
    synonyms = json.loads(target.synonyms)
    if proposal.name not in synonyms:
        synonyms.append(proposal.name)
    target.synonyms = json.dumps(synonyms, ensure_ascii=False)
    session.add(target)
    for row in session.exec(select(WorkTag).where(WorkTag.tag_id == tag_id)).all():
        existing = session.get(WorkTag, (row.work_id, target.id))
        if existing is None:
            row.tag_id = target.id
            session.add(row)
        else:
            session.delete(row)
    proposal.state = "merged"
    proposal.merged_into = target.id
    session.add(proposal)
    session.commit()
    return {"ok": True}


@router.post("/proposals/{tag_id}/reject")
def reject_proposal(tag_id: int, session: SessionDep) -> dict:
    proposal = _proposal(session, tag_id)
    for row in session.exec(select(WorkTag).where(WorkTag.tag_id == tag_id)).all():
        session.delete(row)
    proposal.state = "rejected"
    session.add(proposal)
    session.commit()
    return {"ok": True}


@router.get("/progress")
def tagging_progress(session: SessionDep) -> dict:
    total = session.exec(
        select(func.count()).select_from(Work).where(~Work.hidden.is_(True))
    ).one()
    tagged = session.exec(
        select(func.count(func.distinct(WorkTag.work_id)))
        .join(Work, Work.id == WorkTag.work_id)
        .join(Tag, Tag.id == WorkTag.tag_id)
        .where(~Work.hidden.is_(True), ~WorkTag.removed, Tag.state == "active")
    ).one()
    steps = dict(
        session.exec(
            select(Work.tag_step, func.count())
            .where(~Work.hidden.is_(True))
            .group_by(Work.tag_step)
        ).all()
    )
    pending = session.exec(
        select(func.count()).select_from(Tag).where(Tag.state == "proposed")
    ).one()
    return {
        "total": total,
        "tagged": tagged,
        "by_step": {
            name: steps.get(step, 0)
            for step, name in enumerate(("meta", "desc", "search", "read"), start=1)
        },
        "queued": desc.queued_count(session),
        "pending_proposals": pending,
        "running": desc.is_running(),
    }


@router.post("/run")
def run_tagging(
    session: SessionDep,
    step: Annotated[int, Query(ge=2, le=2)] = 2,
    limit: Annotated[int, Query(ge=1)] = 40,
) -> dict:
    queued = desc.queued_count(session)
    return {"started": desc.start_desc_batch(limit), "queued": queued}


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
