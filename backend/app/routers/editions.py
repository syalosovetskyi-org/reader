"""Издания одного произведения (serg/tasks#3240): связать, отвязать, синхронизация.

Издание — отдельная книга (Work) со своим файлом и прогрессом. Группа только
связывает книги; связь ставит человек, автоматически не связывается ничего —
подсказки «похоже на то же произведение» лишь предлагают кандидатов.
Перенос места между изданиями — backend/app/editions.py.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from .. import editions as ed
from ..bookmeta import norm_lang
from ..db.models import EditionGroup, Progress, Work
from ..db.session import get_session
from .progress import effective_ratio, is_finished

log = logging.getLogger("reader.editions")

router = APIRouter(prefix="/api/editions", tags=["editions"])


def _work(session: Session, work_id: int) -> Work:
    w = session.get(Work, work_id)
    if not w:
        raise HTTPException(404, "work not found")
    return w


def members(session: Session, group_id: int | None) -> list[Work]:
    if not group_id:
        return []
    return list(session.exec(
        select(Work).where(Work.edition_group_id == group_id).order_by(Work.id)
    ).all())


def _progress_of(session: Session, ids: list[int]) -> dict[int, Progress]:
    if not ids:
        return {}
    rows = session.exec(select(Progress).where(Progress.work_id.in_(ids))).all()
    return {int(p.work_id): p for p in rows}


def _stamp(p: Progress | None) -> int:
    """Момент позиции в мс: saved_at клиента, иначе серверное время чтения.

    Пустая позиция (книгу открыли, но места ещё нет) — 0: строку прогресса
    создаёт само открытие, и её свежее время иначе перебивало бы настоящее
    место в другом издании.
    """
    if p is None or not (p.locator or p.text_anchor or p.ratio):
        return 0
    if p.saved_at:
        return int(p.saved_at)
    t = p.last_read_time
    return int(t.timestamp() * 1000) if t else 0


def dissolve_if_single(session: Session, group_id: int | None) -> None:
    """Группа из одного издания — не группа: снять её (без commit).

    Вызывается после отвязки и после удаления книги из библиотеки.
    """
    if not group_id:
        return
    rest = members(session, group_id)
    if len(rest) > 1:
        return
    for w in rest:
        w.edition_group_id = None
        session.add(w)
    session.flush()
    grp = session.get(EditionGroup, group_id)
    if grp:
        session.delete(grp)


def _edition_row(w: Work, prog: Progress | None, current_id: int) -> dict:
    return {
        "id": w.id,
        "title": w.title,
        "author": w.author,
        "language": w.language or "",
        "language_manual": bool(w.language_manual),
        "edition_label": w.edition_label or "",
        "file_format": (w.file_format or "").lower(),
        "converted": w.converted_status == "ready",
        "ratio": effective_ratio(prog, w.chapters_count or 0) if prog else 0.0,
        "chapter": (prog.chapter if prog else "") or "",
        "saved_at": _stamp(prog),
        "position_format": (prog.file_format if prog else "") or "",
        "finished": bool(prog and is_finished(prog, w.chapters_count or 0)),
        "current": w.id == current_id,
    }


def editions_payload(session: Session, work: Work) -> dict:
    eds = members(session, work.edition_group_id) or [work]
    changed = False
    for w in eds:
        before = w.language
        ed.ensure_language(session, w)
        changed |= before != w.language
    if changed:
        session.commit()
    progs = _progress_of(session, [w.id for w in eds])
    rows = [_edition_row(w, progs.get(w.id), work.id) for w in eds]
    grp = session.get(EditionGroup, work.edition_group_id) if work.edition_group_id else None
    latest = max(rows, key=lambda r: r["saved_at"], default=None)
    return {
        "group": {"id": grp.id, "sync": bool(grp.sync)} if grp else None,
        "work_id": work.id,
        "editions": rows,
        "latest_id": latest["id"] if latest and latest["saved_at"] else None,
    }


@router.get("/progress")
def group_progress(session: Session = Depends(get_session)) -> dict[int, float]:
    """Доля для карточек библиотеки: у издания из группы — по последнему
    прочитанному изданию группы (решение Сержа: прогресс карточки — по
    последнему открытому изданию)."""
    works = session.exec(select(Work).where(Work.edition_group_id.is_not(None))).all()
    groups: dict[int, list[Work]] = {}
    for w in works:
        groups.setdefault(int(w.edition_group_id), []).append(w)
    progs = _progress_of(session, [w.id for w in works])
    out: dict[int, float] = {}
    for ws in groups.values():
        if len(ws) < 2:
            continue
        best = max(ws, key=lambda w: _stamp(progs.get(w.id)))
        p = progs.get(best.id)
        if not p:
            continue
        ratio = effective_ratio(p, best.chapters_count or 0)
        for w in ws:
            out[int(w.id)] = ratio
    return out


@router.get("/{work_id}")
def list_editions(work_id: int, session: Session = Depends(get_session)) -> dict:
    """Издания группы книги с прогрессом каждого (без группы — одна книга)."""
    return editions_payload(session, _work(session, work_id))


class LinkIn(BaseModel):
    other_id: int


@router.post("/{work_id}/link")
def link(work_id: int, body: LinkIn, session: Session = Depends(get_session)) -> dict:
    """Связать книгу с другим изданием. Обе остаются отдельными книгами."""
    a = _work(session, work_id)
    b = _work(session, body.other_id)
    if a.id == b.id:
        raise HTTPException(400, "книгу нельзя связать с самой собой")
    ga, gb = a.edition_group_id, b.edition_group_id
    if ga and ga == gb:
        return editions_payload(session, a)
    if ga and gb:
        # Две группы — сливаем ГРУППЫ (книги не трогаем): b-шная уходит в a-шную.
        for w in members(session, gb):
            w.edition_group_id = ga
            session.add(w)
        session.flush()
        old = session.get(EditionGroup, gb)
        if old:
            session.delete(old)
    else:
        gid = ga or gb
        if not gid:
            grp = EditionGroup(sync=True)
            session.add(grp)
            session.flush()
            gid = grp.id
        a.edition_group_id = gid
        b.edition_group_id = gid
        session.add(a)
        session.add(b)
    session.commit()
    session.refresh(a)
    log.info("издания: связаны %s и %s (группа %s)", a.id, b.id, a.edition_group_id)
    return editions_payload(session, a)


@router.delete("/{work_id}/link")
def unlink(work_id: int, session: Session = Depends(get_session)) -> dict:
    """Отвязать книгу от группы. Книга и её прогресс остаются."""
    w = _work(session, work_id)
    gid = w.edition_group_id
    if not gid:
        return editions_payload(session, w)
    w.edition_group_id = None
    session.add(w)
    session.flush()
    dissolve_if_single(session, gid)
    session.commit()
    session.refresh(w)
    log.info("издания: %s отвязана от группы %s", w.id, gid)
    return editions_payload(session, w)


class SyncIn(BaseModel):
    sync: bool


@router.put("/{work_id}/sync")
def set_sync(work_id: int, body: SyncIn, session: Session = Depends(get_session)) -> dict:
    """Включить/выключить синхронизацию позиции для группы книги."""
    w = _work(session, work_id)
    grp = session.get(EditionGroup, w.edition_group_id) if w.edition_group_id else None
    if not grp:
        raise HTTPException(409, "книга не связана с другими изданиями")
    grp.sync = bool(body.sync)
    session.add(grp)
    session.commit()
    return editions_payload(session, w)


class MetaIn(BaseModel):
    language: str | None = Field(default=None, max_length=16)
    edition_label: str | None = Field(default=None, max_length=200)


@router.put("/{work_id}/meta")
def set_meta(work_id: int, body: MetaIn, session: Session = Depends(get_session)) -> dict:
    """Ручная правка языка и метки издания. Пустой язык — снова определять самим."""
    w = _work(session, work_id)
    if body.language is not None:
        raw = body.language.strip()
        lang = norm_lang(raw)
        if raw and not lang:
            raise HTTPException(400, f"не код языка: {raw!r} (ожидается ru, en, uk…)")
        w.language = lang
        w.language_manual = bool(lang)
    if body.edition_label is not None:
        w.edition_label = body.edition_label.strip()
    session.add(w)
    session.commit()
    return editions_payload(session, w)


def _card(w: Work) -> dict:
    return {"id": w.id, "title": w.title, "author": w.author, "language": w.language or "",
            "file_format": (w.file_format or "").lower(), "edition_label": w.edition_label or ""}


@router.get("/{work_id}/suggest")
def suggest(work_id: int, q: str = "", session: Session = Depends(get_session)) -> dict:
    """Кандидаты в издания: «похоже на то же произведение» и поиск по библиотеке.

    Ничего не связывает — связь ставит человек кнопкой.
    """
    w = _work(session, work_id)
    exclude = {x.id for x in members(session, w.edition_group_id)} | {w.id}
    others = [x for x in session.exec(select(Work)).all() if x.id not in exclude]
    scored = sorted(((ed.suggestion_score(w, x), x) for x in others), key=lambda t: -t[0])
    suggested = [x for s, x in scored if s > 0][:8]
    sug_ids = {x.id for x in suggested}
    needle = q.strip().lower()
    if needle:
        rest = [x for x in others if x.id not in sug_ids
                and (needle in (x.title or "").lower() or needle in (x.author or "").lower())]
        suggested = [x for x in suggested
                     if needle in (x.title or "").lower() or needle in (x.author or "").lower()]
    else:
        rest = sorted((x for x in others if x.id not in sug_ids),
                      key=lambda x: x.updated_at, reverse=True)
    return {"suggested": [_card(x) for x in suggested], "others": [_card(x) for x in rest[:20]]}


@router.get("/{work_id}/chapters")
def chapters(work_id: int, fmt: str = "", session: Session = Depends(get_session)) -> dict:
    """Карта глав издания (номера секций — как у foliate)."""
    return ed.chapter_map(_work(session, work_id), fmt)


@router.get("/{work_id}/sync-target")
def sync_target(
    work_id: int,
    fmt: str = "",
    own_saved_at: int = -1,
    session: Session = Depends(get_session),
) -> dict:
    """Куда поставить открытое издание, если в группе есть позиция свежее.

    Позиция группы — последняя по времени среди изданий. own_saved_at — момент
    позиции этого издания, которую читалка получила при открытии: сама
    читалка успевает пересохранить её до этого запроса, и сравнение с базой
    давало бы «своё всегда свежее». fmt — формат, в котором открыто издание.
    """
    w = _work(session, work_id)
    grp = session.get(EditionGroup, w.edition_group_id) if w.edition_group_id else None
    if not grp:
        return {"apply": False, "reason": "no_group"}
    if not grp.sync:
        return {"apply": False, "reason": "sync_off"}
    eds = members(session, grp.id)
    progs = _progress_of(session, [x.id for x in eds])
    own = own_saved_at if own_saved_at >= 0 else _stamp(progs.get(w.id))
    src = max((x for x in eds if x.id != w.id and progs.get(x.id)),
              key=lambda x: _stamp(progs.get(x.id)), default=None)
    if not src or _stamp(progs[src.id]) <= own:
        return {"apply": False, "reason": "own_is_latest"}
    sp = progs[src.id]
    if not (sp.locator or sp.text_anchor or sp.ratio):
        return {"apply": False, "reason": "source_empty"}
    for x in (w, src):
        ed.ensure_language(session, x)
    session.commit()
    mapped = ed.map_position(src, sp, sp.file_format or "", w, fmt)
    return {
        "apply": True,
        "source_id": src.id,
        "source_format": (sp.file_format or src.file_format or "").lower(),
        "source_language": src.language or "",
        "source_label": src.edition_label or "",
        "saved_at": _stamp(sp),
        **mapped,
    }
