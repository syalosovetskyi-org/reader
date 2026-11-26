"""Хранение разметки: транзакцией и commit управляет вызывающий код."""

from __future__ import annotations

import json
import logging

from sqlmodel import Session, select

from ..db.models import Tag, Work, WorkTag, utcnow
from .meta import tag_from_metadata
from .vocab import VOCAB

log = logging.getLogger("reader.tagging.store")


def visible_work_tags(session: Session, work_id: int) -> list[dict]:
    return [
        {"id": tag.id, "name": tag.name, "group": tag.group, "source": source}
        for tag, source in session.exec(
            select(Tag, WorkTag.source)
            .join(WorkTag, WorkTag.tag_id == Tag.id)
            .where(
                WorkTag.work_id == work_id,
                WorkTag.removed.is_(False),
                Tag.state == "active",
            )
            .order_by(Tag.id)
        ).all()
    ]


def sync_vocab(session: Session) -> None:
    existing = {tag.name: tag for tag in session.exec(select(Tag)).all()}
    for group, entries in VOCAB.items():
        for name, aliases in entries.items():
            tag = existing.get(name)
            if tag is None:
                tag = Tag(name=name, group=group)
            tag.group = group
            # Синонимы одобренных merge должны переживать перезапуск приложения.
            tag.synonyms = json.dumps(
                list(dict.fromkeys([*aliases, *json.loads(tag.synonyms)])),
                ensure_ascii=False,
            )
            tag.state = "active"
            tag.merged_into = None
            session.add(tag)
    session.flush()


def apply_meta_tags(session: Session, work: Work) -> None:
    result = tag_from_metadata(work)
    session.add(work)
    session.flush()  # новой книге нужен id до записи WorkTag
    tags = {
        (tag.group, tag.name): tag.id
        for tag in session.exec(select(Tag).where(Tag.state == "active")).all()
    }
    wanted = {tags[key] for key in result.tags}
    existing = {
        row.tag_id: row
        for row in session.exec(select(WorkTag).where(WorkTag.work_id == work.id)).all()
    }
    for tag_id, row in existing.items():
        if row.source == "meta" and not row.removed:
            if tag_id not in wanted:
                session.delete(row)
            else:
                row.confidence = 1.0
                session.add(row)
    for tag_id in wanted - existing.keys():
        session.add(
            WorkTag(work_id=work.id, tag_id=tag_id, source="meta", confidence=1.0)
        )
    now = utcnow()
    if work.write_status_source != "manual":
        work.write_status = result.status
        work.write_status_source = "meta" if result.status else ""
        work.write_status_checked_at = now
    work.tag_step = max(work.tag_step, 1)
    work.tagged_at = now
    session.add(work)


def try_apply_meta_tags(session: Session, work: Work) -> None:
    """Сбой шага 1 не отменяет импорт: все его записи защищены SAVEPOINT."""
    session.add(work)
    # begin_nested сам flush-ит внешние изменения: сохраняем метаданные книги
    # вне SAVEPOINT, чтобы rollback тегирования не забрал их с собой.
    session.flush()
    work_id, title = work.id, work.title
    try:
        with session.begin_nested():
            apply_meta_tags(session, work)
    except Exception as exc:  # noqa: BLE001 — импорт должен продолжиться
        log.warning("Тегирование метаданных книги %s (%s): %s", work_id, title, exc)


def merge_work_tags(session: Session, keep: Work, drop: Work) -> None:
    """При дедупе сохранять разметку и ручные запреты обеих записей."""
    existing = {
        row.tag_id: row
        for row in session.exec(select(WorkTag).where(WorkTag.work_id == keep.id)).all()
    }
    for row in session.exec(select(WorkTag).where(WorkTag.work_id == drop.id)).all():
        target = existing.get(row.tag_id)
        if target is None:
            row.work_id = keep.id
            session.add(row)
        else:
            target.removed = target.removed or row.removed
            if row.source == "manual" or target.source == "meta":
                target.source = row.source
                target.confidence = row.confidence
            session.add(target)
            session.delete(row)
    # Сначала перенос/удаление связей, затем вызывающий удалит книгу: иначе
    # ON DELETE CASCADE успеет удалить строки до ORM и даст двойной DELETE.
    session.flush()
