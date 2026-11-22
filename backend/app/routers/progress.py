"""Роутер прогресса чтения: получить/сохранить позицию.

Прогресс хранится как ratio (0..1, совместимо с ReadEra) + точный locator для
foliate-js. На этапе 3 этот же прогресс реконсилится с бэкапом ReadEra.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from ..book_revision import file_rev
from sqlmodel import Session, select

from ..db.models import PositionHistory, Progress, Work, utcnow
from ..db.session import get_session

router = APIRouter(prefix="/api/progress", tags=["progress"])


# Сколько прошлых позиций держим на книгу. Список переходов — рабочий
# инструмент, а не архив: глубже нескольких десятков в него не заглядывают,
# зато каждая запись стоит строки в БД на каждую книгу библиотеки.
MAX_HISTORY = 50
# Порог доли для СТАРОГО фронта (тело PUT без session_id — закэшированный
# service worker'ом код до serg/tasks#3239): только по нему прежнее место
# уезжает в историю. Новый фронт сообщает о переходах сам, а смену вкладки или
# устройства сервер узнаёт по base_rev/session_id, а не по разнице доли.
# Временная ветка: уберём, когда старых клиентов не останется.
JUMP_RATIO = 0.005


def same_place(a_locator: str, a_anchor: str, b_locator: str, b_anchor: str,
               a_ratio: float = 0.0, b_ratio: float = 0.0) -> bool:
    """Одно ли это место книги: тот же locator или тот же текстовый якорь.

    Якорь сравнивается и по префиксу: старые записи хранят 12 слов, новые — 24
    (как sameAnchor в reader-core.js). У позиций без координат (ReadEra) —
    по доле."""
    if a_locator and a_locator == b_locator:
        return True
    if a_anchor and b_anchor and (a_anchor.startswith(b_anchor) or b_anchor.startswith(a_anchor)):
        return True
    if not (a_locator or a_anchor or b_locator or b_anchor):
        return abs(float(a_ratio or 0.0) - float(b_ratio or 0.0)) < 1e-9
    return False


def _add_snapshot(
    session: Session,
    work_id: int,
    *,
    ratio: float,
    locator: str,
    text_anchor: str,
    chapter: str,
    reason: str,
    device: str = "",
    device_id: str = "",
    book_rev: str = "",
    client_id: str = "",
) -> PositionHistory | None:
    """Положить позицию в историю книги. Возвращает строку (новую или уже
    лежащую сверху с тем же местом) либо None, если координат нет вовсе."""
    if not (locator or text_anchor or ratio):
        return None  # пустая позиция: возвращаться в неё некуда
    last = session.exec(
        select(PositionHistory)
        .where(PositionHistory.work_id == work_id)
        .order_by(PositionHistory.created_at.desc(), PositionHistory.id.desc())
    ).first()
    # То же место подряд — не новая позиция, а повтор: она уже есть в истории,
    # значит и не потеряна.
    if last and same_place(last.locator, last.text_anchor, locator, text_anchor,
                           last.ratio, ratio):
        return last
    row = PositionHistory(
        work_id=work_id,
        ratio=float(ratio or 0.0),
        locator=locator or "",
        text_anchor=text_anchor or "",
        chapter=chapter or "",
        reason=reason,
        device=device or "",
        device_id=device_id or "",
        book_rev=book_rev or "",
        client_id=client_id or "",
    )
    session.add(row)
    session.flush()
    _trim_history(session, work_id)
    return row


def _push_history(
    session: Session,
    prog: Progress,
    *,
    chapter: str | None = None,
    reason: str,
    device: str | None = None,
) -> PositionHistory | None:
    """Сохранить ТЕКУЩУЮ позицию Progress в историю (до того, как её перезапишут).

    device по умолчанию — имя того, кто ЧИТАЛ это место (prog.device), а не
    того, кто его сдвинул."""
    return _add_snapshot(
        session,
        prog.work_id,
        ratio=float(prog.ratio or 0.0),
        locator=prog.locator or "",
        text_anchor=prog.text_anchor or "",
        chapter=prog.chapter if chapter is None else chapter,
        reason=reason,
        device=prog.device if device is None else device,
        device_id=prog.device_id or "",
        book_rev=prog.book_rev or "",
    )


def bump_rev(prog: Progress) -> None:
    """Позицию сдвинули — следующая запись с прежней base_rev будет чужой."""
    prog.rev = int(prog.rev or 0) + 1


def _trim_history(session: Session, work_id: int) -> None:
    """Оставить последние MAX_HISTORY записей книги."""
    rows = session.exec(
        select(PositionHistory)
        .where(PositionHistory.work_id == work_id)
        .order_by(PositionHistory.created_at.desc(), PositionHistory.id.desc())
    ).all()
    for extra in rows[MAX_HISTORY:]:
        session.delete(extra)


class ProgressIn(BaseModel):
    ratio: float = Field(ge=0.0, le=1.0)
    locator: str = ""
    text_anchor: str = ""
    # Название главы на момент сохранения: в Progress не хранится (там только
    # координаты), но нужно истории позиций — подписью к записи.
    chapter: str = ""
    # SHA-1 открытого файла: CFI точен только для него (см. Progress.book_rev).
    book_rev: str = ""
    file_format: str = ""
    # Момент ПЕРЕЛИСТЫВАНИЯ на клиенте, мс от эпохи (не момент отправки).
    saved_at: int = 0
    # Читалка показала последнюю страницу книги.
    at_end: bool = False
    # Досылка позиции, сохранённой без сети: старее серверной — не применяется,
    # но и не пропадает (строка offline в истории).
    replay: bool = False
    # Опознание писателя (serg/tasks#3239). Пустой session_id — старый фронт.
    session_id: str = ""
    device_id: str = ""
    device: str = ""
    # Ревизия Progress.rev, от которой клиент читал. None — не знает (старый
    # фронт или позиция не загрузилась).
    base_rev: int | None = None


class HistoryIn(BaseModel):
    """Снимок позиции, который фронт кладёт в историю ПЕРЕД прыжком."""

    ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    locator: str = ""
    text_anchor: str = ""
    chapter: str = ""
    reason: str = "jump"
    # SHA-1 файла, в котором посчитан locator: возврат по CFI точен только в нём.
    book_rev: str = ""
    device: str = ""
    device_id: str = ""
    # Идемпотентность: повтор с тем же client_id возвращает уже лежащую строку.
    client_id: str = ""


def effective_ratio(prog: Progress, chapters_now: int) -> float:
    """Доля прочитанного с поправкой на выросшую книгу.

    Хранимый ratio — доля от объёма НА МОМЕНТ ЧТЕНИЯ. Когда FanFicFare докачал
    новые главы, та же позиция стала меньшей долей книги, но в базе осталось
    прежнее число. Из-за этого дочитанная книга навсегда оставалась «дочитанной»
    (ratio >= 0.98), карточка прятала плашку обновления — и новые главы человек
    просто не видел. Карточка на это и рассчитывала: «докачается глава — ratio
    упадёт ниже порога и плашка вернётся сама», только падать было нечему.

    Пересчёт пропорционален числу глав: приблизительно (главы разного размера),
    но достаточно, чтобы книга перестала считаться дочитанной. Хранимое значение
    не трогаем — оно уезжает в ReadEra как есть; правим только то, что отдаём.
    """
    ratio = float(prog.ratio or 0.0)
    was = int(prog.chapters_at_read or 0)
    now = int(chapters_now or 0)
    if was <= 0 or now <= was:
        return ratio
    return max(0.0, min(1.0, ratio * was / now))


@router.get("")
def all_progress(session: Session = Depends(get_session)) -> dict[int, float]:
    """Все позиции разом {work_id: ratio} — чтобы фронт не делал N запросов на список книг."""
    rows = session.exec(select(Progress)).all()
    chapters = {
        int(w_id): int(cnt or 0)
        for w_id, cnt in session.exec(select(Work.id, Work.chapters_count)).all()
    }
    return {
        int(p.work_id): effective_ratio(p, chapters.get(int(p.work_id), 0))
        for p in rows
    }


def is_finished(prog: Progress, chapters_now: int) -> bool:
    """Дочитана ли книга: показана последняя страница, и глав с тех пор не прибавилось."""
    if not prog.finished_at:
        return False
    return int(chapters_now or 0) <= int(prog.finished_chapters or 0) or not chapters_now


@router.get("/finished")
def finished(session: Session = Depends(get_session)) -> list[int]:
    """id дочитанных книг — для карточек библиотеки (serg/tasks#1853).

    Раньше карточка выводила «прочитано» из доли: «осталось меньше главы» — и
    книга с половиной непрочитанной последней главы уже стояла прочитанной."""
    rows = session.exec(select(Progress).where(Progress.finished_at.is_not(None))).all()
    chapters = {
        int(w_id): int(cnt or 0)
        for w_id, cnt in session.exec(select(Work.id, Work.chapters_count)).all()
    }
    return [
        int(p.work_id) for p in rows if is_finished(p, chapters.get(int(p.work_id), 0))
    ]


class FinishedIn(BaseModel):
    finished: bool


@router.put("/{work_id}/finished")
def set_finished(
    work_id: int, body: FinishedIn, session: Session = Depends(get_session)
) -> dict:
    """Ручная отметка «прочитано» со страницы книги (serg/tasks#1853).

    Автоматически книга становится прочитанной только на последней странице; у
    книг с хвостом «об авторе»/послесловием до неё не листают — для них и
    нужна ручная отметка. Позицию чтения не трогает."""
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "work not found")
    prog = session.exec(select(Progress).where(Progress.work_id == work_id)).first()
    if not prog:
        prog = Progress(work_id=work_id, chapters_at_read=int(work.chapters_count or 0))
    if body.finished:
        prog.finished_at = utcnow()
        prog.finished_chapters = int(work.chapters_count or 0)
    else:
        prog.finished_at = None
        prog.finished_chapters = 0
    session.add(prog)
    session.commit()
    return {"ok": True, "work_id": work_id, "finished": body.finished}


@router.get("/{work_id}")
def get_progress(work_id: int, session: Session = Depends(get_session)) -> Progress:
    prog = session.exec(select(Progress).where(Progress.work_id == work_id)).first()
    if not prog:
        # Пустой прогресс по умолчанию (книга ещё не открывалась).
        return Progress(work_id=work_id, ratio=0.0, locator="", source="web")
    # Доля — с поправкой на выросшую книгу; locator и якорь возвращаем как есть,
    # позиция восстанавливается по ним, а не по доле.
    work = session.get(Work, work_id)
    prog.ratio = effective_ratio(prog, work.chapters_count if work else 0)
    return prog


def _progress_out(prog: Progress, *, applied: bool, snapshot: PositionHistory | None) -> dict:
    """Ответ PUT: позиция сервера + применена ли запись + id снимка истории."""
    out = jsonable_encoder(prog)
    out["applied"] = applied
    out["history_id"] = snapshot.id if snapshot is not None else None
    return out


def _ua_family(request: Request) -> str:
    """Семейство ОС по User-Agent — только для старого фронта без имени устройства."""
    ua = request.headers.get("user-agent", "").lower()
    families = [("android", "android"), ("iphone", "ios"), ("ipad", "ios"),
                ("windows", "windows"), ("macintosh", "macos"), ("linux", "linux")]
    return next((name for token, name in families if token in ua), "unknown")


@router.put("/{work_id}")
def set_progress(
    work_id: int,
    body: ProgressIn,
    request: Request,
    session: Session = Depends(get_session),
):
    """Записать позицию чтения.

    Переход (оглавление, сноска, поиск…) клиент кладёт в историю сам, через
    POST /history. Здесь история пишется, только когда позицию за спиной
    клиента сдвинули: его base_rev устарела, вкладка другая и место другое.
    Тогда прежняя позиция уезжает в историю с подписью, где её читали.
    """
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "work not found")

    import time as _time

    saved_at = int(body.saved_at or 0) or int(_time.time() * 1000)
    prog = session.exec(select(Progress).where(Progress.work_id == work_id)).first()
    rev = file_rev(work)
    if (prog and not body.replay and body.book_rev and rev
            and body.book_rev != rev and prog.book_rev == rev):
        return JSONResponse(
            status_code=409,
            content={"progress": jsonable_encoder(prog), "file_rev": rev},
        )
    snapshot: PositionHistory | None = None
    if prog:
        here = same_place(prog.locator, prog.text_anchor, body.locator, body.text_anchor,
                          prog.ratio, body.ratio)
        if body.replay and saved_at < int(prog.saved_at or 0) and not here:
            # Офлайн-хвост старее серверной позиции: затирать её нельзя, но и
            # молча выбросить нельзя — кладём в историю, откуда можно вернуться.
            snapshot = _add_snapshot(
                session, work_id, ratio=body.ratio, locator=body.locator,
                text_anchor=body.text_anchor, chapter=body.chapter, reason="offline",
                device=body.device, device_id=body.device_id, book_rev=body.book_rev,
            )
            session.commit()
            session.refresh(prog)
            return _progress_out(prog, applied=False, snapshot=snapshot)
        if body.session_id:
            fresh = (bool(prog.session_id) and prog.session_id == body.session_id) or (
                body.base_rev is not None and body.base_rev == int(prog.rev or 0))
            if not fresh and not here:
                reason = "tab" if prog.device_id and prog.device_id == body.device_id else "device"
                snapshot = _push_history(session, prog, reason=reason)
        elif abs(float(prog.ratio or 0.0) - float(body.ratio)) >= JUMP_RATIO:
            # Старый фронт (нет session_id): прежнее правило по доле, но без
            # условия про смену главы — оно писало строку на каждой границе главы.
            snapshot = _push_history(session, prog, reason="overwrite",
                                     device=prog.device or _ua_family(request))
        far = abs(float(prog.ratio or 0.0) - float(body.ratio)) >= JUMP_RATIO
        prog.ratio = body.ratio
        # Координаты пишутся ОДНОЙ тройкой. Раньше пустой якорь оставлял
        # прежний — и строка склеивалась из двух страниц: CFI новой, якорь
        # старой; восстановление по якорю возвращало на старое место. Пустой
        # якорь (картинка, пустая страница) теперь значит «восстанавливай по CFI».
        prog.locator = body.locator
        prog.text_anchor = body.text_anchor
        prog.book_rev = body.book_rev
        prog.file_format = body.file_format
        # Глава пришла — пишем. Пришла пустая (обложка, титул или
        # релокейт без tocItem) — сохраняем прежнюю подпись ТОЛЬКО пока читают
        # рядом. Далеко ушли — прежняя глава уже не про эту позицию.
        if body.chapter or far:
            prog.chapter = body.chapter
        prog.last_read_time = utcnow()
        prog.chapters_at_read = int(work.chapters_count or 0)
        prog.saved_at = saved_at
        prog.source = "web"
    else:
        prog = Progress(
            work_id=work_id,
            ratio=body.ratio,
            locator=body.locator,
            text_anchor=body.text_anchor,
            book_rev=body.book_rev,
            file_format=body.file_format,
            chapter=body.chapter,
            chapters_at_read=int(work.chapters_count or 0),
            saved_at=saved_at,
            source="web",
        )
        session.add(prog)
    prog.session_id = body.session_id
    prog.device_id = body.device_id
    prog.device = body.device or ("" if body.session_id else _ua_family(request))
    bump_rev(prog)
    # «Дочитано» ставится только фактом последней страницы и не снимается
    # перелистыванием назад (перечитать любимую главу — не «не дочитал»).
    # Снимают его новые главы: finished_chapters < chapters_count.
    if body.at_end:
        prog.finished_at = utcnow()
        prog.finished_chapters = int(work.chapters_count or 0)

    # Отметим время последней активности на самой работе (для сортировки/sync).
    # Это НЕ «дата выхода новых глав» — для неё есть content_updated_at.
    work.updated_at = utcnow()
    session.add(work)
    session.commit()
    session.refresh(prog)
    if snapshot is not None:
        session.refresh(snapshot)
    return _progress_out(prog, applied=True, snapshot=snapshot)


@router.get("/{work_id}/history")
def get_history(
    work_id: int, session: Session = Depends(get_session)
) -> list[PositionHistory]:
    """Прошлые позиции книги, новые первыми."""
    return list(
        session.exec(
            select(PositionHistory)
            .where(PositionHistory.work_id == work_id)
            .order_by(PositionHistory.created_at.desc(), PositionHistory.id.desc())
            .limit(MAX_HISTORY)
        ).all()
    )


@router.post("/{work_id}/history")
def add_history(
    work_id: int,
    body: HistoryIn,
    session: Session = Depends(get_session),
) -> PositionHistory:
    """Положить позицию в историю (читалка зовёт это после явного перехода —
    оглавление, сноска, поиск…). Повтор с тем же client_id — та же строка."""
    if not session.get(Work, work_id):
        raise HTTPException(404, "work not found")
    if body.client_id:
        seen = session.exec(
            select(PositionHistory)
            .where(PositionHistory.work_id == work_id)
            .where(PositionHistory.client_id == body.client_id)
        ).first()
        if seen:
            return seen
    row = _add_snapshot(
        session, work_id, ratio=body.ratio, locator=body.locator,
        text_anchor=body.text_anchor, chapter=body.chapter,
        reason=body.reason or "jump", device=body.device, device_id=body.device_id,
        book_rev=body.book_rev, client_id=body.client_id,
    )
    if row is None:
        raise HTTPException(422, "позиция без координат: locator, text_anchor и ratio пусты")
    session.commit()
    session.refresh(row)
    return row


@router.delete("/{work_id}/history")
def clear_history(work_id: int, session: Session = Depends(get_session)) -> dict:
    """Очистить историю переходов книги."""
    rows = session.exec(
        select(PositionHistory).where(PositionHistory.work_id == work_id)
    ).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return {"deleted": len(rows)}
