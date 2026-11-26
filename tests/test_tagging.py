"""Шаг 1 тегирования: словарь, ручные запреты, транзакции и API (#3285)."""

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine, select

from backend.app.db.models import Tag, Work, WorkTag
from backend.app.db.session import get_session
from backend.app.routers import library, tags
from backend.app.tagging import store
from backend.app.tagging.meta import (
    classify,
    normalize,
    normalize_fandom,
    tag_from_metadata,
)
from backend.app.tagging.vocab import VOCAB


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Научная фантастика", [("Жанр", "Фантастика")]),
        ("sf_social", [("Жанр", "Социальная фантастика")]),
        ("Попаданцы: В чужом теле", [("Жанр", "Попаданцы")]),
        ("Last Update Year/Month: 2026/06", "drop"),
        ("Taylor Hebert | Skitter", None),
        ("Programming", [("IT: темы", "Программирование")]),
        ("python", [("IT: языки и технологии", "Python")]),
        (
            "Rational Fiction",
            [("Форма", "Веб-сериал"), ("Темы и тропы", "Рационалист")],
        ),
        ("science", [("Форма", "Нон-фикшн"), ("Нон-фикшн", "Наука")]),
        ("[]", "drop"),
        ("", None),
    ],
)
def test_classify(label, expected):
    assert classify(label) == expected


def test_normalize_and_exact_priority():
    assert normalize(" «[  ТвЁрдая \n НФ ]» ") == "твердая нф"
    assert normalize("drama (play)") == "drama (play)"
    assert classify("Programming") == [("IT: темы", "Программирование")]
    assert normalize_fandom("Parahumans Series - Wildbow") == "Worm (Wildbow)"
    assert normalize_fandom("Приключения Эраста Фандорина") is None


@pytest.mark.parametrize(
    "genres",
    [
        '["Научная фантастика", "Programming"]',
        "Научная фантастика, Programming",
        'TAGS: ["Научная фантастика", "Programming"]',
    ],
)
def test_genre_formats(genres):
    result = tag_from_metadata(Work(genres=genres, rating="18+"))
    assert result.tags == [
        ("Жанр", "Фантастика"),
        ("IT: темы", "Программирование"),
        ("Контент", "18+"),
    ]
    assert result.unknown_labels == []


@pytest.mark.parametrize(
    "genres", ["TAGS: Rational Fiction", '["TAGS: Rational Fiction"]']
)
def test_calibre_prefix(genres):
    assert len(tag_from_metadata(Work(genres=genres)).tags) == 2


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"site": "ficbook", "status": "в процессе"}, "wip"),
        ({"status": "завершён"}, "done"),
        ({"genres": "Complete"}, "done"),
        ({"genres": "abandoned"}, "dead"),
        ({"genres": "Complete", "status": "в процессе"}, "wip"),
        ({"genres": "Complete", "status": "завершён", "site": "pythondocs"}, ""),
    ],
)
def test_status(kwargs, status):
    result = tag_from_metadata(Work(**kwargs))
    assert result.status == status
    assert result.unknown_labels == []


def test_fandom_series_unknown_and_deduplication():
    work = Work(
        genres='["python", "Python", "fiction", "Taylor Hebert | Skitter"]',
        fandom="Приключения Эраста Фандорина",
    )
    result = tag_from_metadata(work)
    assert result.tags == [("IT: языки и технологии", "Python")]
    assert result.unknown_labels == [
        "Taylor Hebert | Skitter",
        "Приключения Эраста Фандорина",
    ]
    assert work.fandom == "Приключения Эраста Фандорина"
    assert tag_from_metadata(Work(fandom="Worm")).unknown_labels == []
    assert tag_from_metadata(Work(fandom="Worm")).tags == []


def test_sync_vocab_idempotent_and_updates_synonyms(session):
    store.sync_vocab(session)
    before = {t.name: (t.id, t.created_at) for t in session.exec(select(Tag)).all()}
    tag = session.exec(select(Tag).where(Tag.name == "Python")).one()
    tag.synonyms, tag.state, tag.group = "[]", "proposed", "wrong"
    session.add(tag)
    store.sync_vocab(session)
    after = {t.name: (t.id, t.created_at) for t in session.exec(select(Tag)).all()}
    assert before == after
    assert len(after) == sum(len(group) for group in VOCAB.values())
    assert tag.state == "active" and tag.group == "IT: языки и технологии"
    assert json.loads(tag.synonyms) == VOCAB[tag.group][tag.name]


def _tag(session, name):
    return session.exec(select(Tag).where(Tag.name == name)).one()


def test_apply_idempotent_refresh_and_cascade(session):
    store.sync_vocab(session)
    work = Work(genres="Python, Programming, complete", tag_step=3)
    store.apply_meta_tags(session, work)
    session.commit()
    before = [r.model_dump() for r in session.exec(select(WorkTag)).all()]
    store.apply_meta_tags(session, work)
    session.commit()
    assert [r.model_dump() for r in session.exec(select(WorkTag)).all()] == before
    assert work.write_status == "done" and work.write_status_source == "meta"
    assert work.write_status_checked_at and work.tagged_at and work.tag_step == 3
    work.genres = "Фэнтези"
    store.apply_meta_tags(session, work)
    session.commit()
    assert [r.tag_id for r in session.exec(select(WorkTag)).all()] == [
        _tag(session, "Фэнтези").id
    ]
    assert work.write_status == "" and work.write_status_source == ""


@pytest.mark.parametrize("source", ["meta", "manual", "desc"])
def test_tombstones_and_other_sources_and_manual_status(session, source):
    store.sync_vocab(session)
    work = Work(
        genres="Python, Programming", write_status="dead", write_status_source="manual"
    )
    session.add(work)
    session.flush()
    python = _tag(session, "Python")
    programming = _tag(session, "Программирование")
    session.add(WorkTag(work_id=work.id, tag_id=python.id, source=source, removed=True))
    session.add(
        WorkTag(work_id=work.id, tag_id=programming.id, source="desc", confidence=0.8)
    )
    for genres in ("Python, Programming, Complete", "", "Python, Programming"):
        work.genres = genres
        store.apply_meta_tags(session, work)
        session.commit()
        assert session.get(WorkTag, (work.id, python.id)).removed
        desc = session.get(WorkTag, (work.id, programming.id))
        assert desc.source == "desc" and desc.confidence == 0.8
        assert work.write_status == "dead" and work.write_status_source == "manual"
        assert work.write_status_checked_at is None


def test_tagging_failure_rolls_back_only_tags(session, monkeypatch, caplog):
    store.sync_vocab(session)
    session.commit()
    work = Work(title="Книга со сбоем", genres="Python")
    original = store.apply_meta_tags

    def broken(session, work):
        original(session, work)
        session.flush()
        # Настоящая ошибка БД, после которой сессии необходим rollback.
        session.add(Tag(name="Python", group="wrong"))
        session.flush()

    monkeypatch.setattr(store, "apply_meta_tags", broken)
    monkeypatch.setattr(store.log, "handlers", [caplog.handler])
    caplog.set_level(logging.WARNING)
    store.try_apply_meta_tags(session, work)
    session.commit()
    assert session.get(Work, work.id).genres == "Python"
    assert work.tag_step == 0 and work.tagged_at is None
    assert session.exec(select(WorkTag)).all() == []
    assert "Книга со сбоем" in caplog.text and "UNIQUE" in caplog.text


@pytest.fixture
def tag_api(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tags.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        store.sync_vocab(session)
        session.commit()

    def get_test_session():
        with Session(engine) as session:
            yield session

    app = FastAPI()
    app.include_router(library.router)
    app.include_router(tags.router)
    app.dependency_overrides[get_session] = get_test_session
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def test_api_tags_status_counts_and_no_n_plus_one(tag_api):
    client, engine = tag_api
    with Session(engine) as session:
        works = [
            Work(title=f"Книга {i}", genres="Python, Complete", hidden=(i == 1))
            for i in range(12)
        ]
        for work in works:
            store.apply_meta_tags(session, work)
        session.flush()
        python_id = _tag(session, "Python").id
        removed = _tag(session, "Фэнтези")
        proposed = Tag(name="Предложен", group="Жанр", state="proposed")
        session.add(proposed)
        session.flush()
        session.add(WorkTag(work_id=works[0].id, tag_id=removed.id, removed=True))
        session.add(WorkTag(work_id=works[0].id, tag_id=proposed.id, source="desc"))
        session.commit()
        wid, hidden_id = works[0].id, works[1].id
    detail = client.get(f"/api/library/{wid}")
    assert detail.status_code == 200
    assert detail.json()["write_status"] == "done"
    assert detail.json()["tags"] == [
        {
            "id": python_id,
            "name": "Python",
            "group": "IT: языки и технологии",
            "source": "meta",
        }
    ]
    queries = []

    def capture(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get("/api/library")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200
    assert len(queries) == 2  # книги и все их теги
    assert len(response.json()) == 11
    assert all(
        w["write_status"] == "done" and w["tag_ids"] == [python_id]
        for w in response.json()
    )
    hidden = client.get("/api/library?hidden=true").json()
    assert (
        len(hidden) == 1
        and hidden[0]["id"] == hidden_id
        and hidden[0]["tag_ids"] == [python_id]
    )
    tag_list = client.get("/api/tags").json()
    assert next(t for t in tag_list if t["id"] == python_id)["count"] == 11
    assert next(t for t in tag_list if t["name"] == "Фэнтези")["count"] == 0
    assert all(t["name"] != "Предложен" for t in tag_list)
    assert [(t["group"], t["name"]) for t in tag_list] == [
        (group, name) for group, entries in VOCAB.items() for name in entries
    ]
    assert client.delete(f"/api/library/{wid}").status_code == 200
    with Session(engine) as session:
        assert session.exec(select(WorkTag).where(WorkTag.work_id == wid)).all() == []


def test_backfill_dry_run_no_writes_and_statistics(tag_api):
    client, engine = tag_api
    with Session(engine) as session:
        session.add_all(
            [
                Work(genres="Python, Programming, 18+, Complete, Mystery label"),
                Work(genres="[]"),
                Work(genres="fiction, Mystery label", hidden=True),
            ]
        )
        session.commit()
        before = session.exec(select(Work)).all()
        snapshots = [w.model_dump() for w in before]
    mutations = []

    def capture(conn, cursor, statement, params, context, executemany):
        if not statement.lstrip().upper().startswith("SELECT"):
            mutations.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.post("/api/tags/backfill?dry_run=true")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert mutations == []
    assert response.json() == {
        "books": 3,
        "with_any_tag": 1,
        "with_genre": 1,
        "with_3plus": 1,
        "without_labels": 1,
        "status_counts": {"": 2, "done": 1, "wip": 0, "dead": 0},
        "unknown_labels": [["Mystery label", 2]],
    }
    with Session(engine) as session:
        assert [w.model_dump() for w in session.exec(select(Work)).all()] == snapshots
        assert session.exec(select(WorkTag)).all() == []
    assert client.post("/api/tags/backfill?dry_run=false").json() == response.json()
    with Session(engine) as session:
        assert len(session.exec(select(WorkTag)).all()) == 3
        assert all(
            w.tag_step == 1 and w.tagged_at for w in session.exec(select(Work)).all()
        )


def test_existing_work_schema_auto_migration(tmp_path, monkeypatch):
    from backend.app.db import session as db

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE work (id INTEGER PRIMARY KEY, title VARCHAR)")
        )
        connection.execute(text("INSERT INTO work (id, title) VALUES (1, 'old')"))
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    db.init_db()
    with Session(engine) as session:
        work = session.get(Work, 1)
        assert work.write_status == "" and work.write_status_source == ""
        assert (
            work.write_status_checked_at is None
            and work.tagged_at is None
            and work.tag_step == 0
        )
        assert len(session.exec(select(Tag)).all()) == sum(map(len, VOCAB.values()))
    engine.dispose()


@pytest.mark.parametrize("failure", [False, True])
def test_download_registration_tags_metadata_without_network(
    session, tmp_path, monkeypatch, failure
):
    from backend.app import bookmeta, services
    from backend.downloaders.base import DownloadResult

    store.sync_vocab(session)
    path = tmp_path / "metadata.fb2"
    path.write_text("<book>Текст</book>")
    monkeypatch.setattr(services, "import_file", lambda *args: (path, False))
    monkeypatch.setattr(services.calibre, "add_book", lambda *args: None)
    monkeypatch.setattr(services.covers, "extract_cover", lambda *args: None)
    monkeypatch.setattr(services, "_push_readera", lambda *args: None)
    monkeypatch.setattr(
        bookmeta,
        "extract_meta",
        lambda *args: {"genres": '["Python"]', "status": "Complete"},
    )
    if failure:

        def fail(*args):
            raise RuntimeError("tagging failed")

        monkeypatch.setattr(store, "apply_meta_tags", fail)
    work = services.register_download(
        DownloadResult(path, "fb2", title="Новая книга"), session
    )
    assert work.id and "Python" in work.genres
    rows = session.exec(select(WorkTag).where(WorkTag.work_id == work.id)).all()
    assert len(rows) == (0 if failure else 1)
    assert work.write_status == ("" if failure else "done")


@pytest.mark.parametrize("kind", ["new", "matched", "existing"])
def test_calibre_sync_tags_changed_genres(session, monkeypatch, kind):
    from backend.calibre import sync

    store.sync_vocab(session)
    if kind != "new":
        session.add(
            Work(
                title="Книга Python",
                author="Автор",
                calibre_id=42 if kind == "existing" else None,
            )
        )
        session.commit()
    monkeypatch.setattr(sync.client, "http_mode", lambda: True)
    monkeypatch.setattr(
        sync.client,
        "iter_opds_books",
        lambda: iter(
            [
                {
                    "calibre_id": 42,
                    "title": "Книга Python",
                    "authors": "Автор",
                    "formats": ["EPUB"],
                    "tags": ["Programming", "Python"],
                }
            ]
        ),
    )
    monkeypatch.setattr(sync.client, "best_format", lambda formats: "epub")
    sync.sync_catalog(session)
    work = session.exec(select(Work).where(Work.calibre_id == 42)).one()
    assert work.tag_step == 1
    assert (
        len(session.exec(select(WorkTag).where(WorkTag.work_id == work.id)).all()) == 2
    )


def test_api_queries_directly_in_transaction(session):
    """Проверить SQL и ответы отдельно от HTTP-транспорта и его потоков."""
    store.sync_vocab(session)
    works = [
        Work(genres="Python, Programming, Complete"),
        Work(hidden=True, genres="Python"),
    ]
    for work in works:
        store.apply_meta_tags(session, work)
    proposed = Tag(name="Предложенный", group="Жанр", state="proposed")
    session.add(proposed)
    session.flush()
    session.add_all(
        [
            WorkTag(work_id=works[0].id, tag_id=proposed.id, source="desc"),
            WorkTag(
                work_id=works[0].id, tag_id=_tag(session, "Фэнтези").id, removed=True
            ),
        ]
    )
    session.commit()
    queries = []

    def capture(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        listed = library.list_works(session=session)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(queries) == 2
    assert len(listed) == 1 and listed[0]["write_status"] == "done"
    assert len(listed[0]["tag_ids"]) == 2
    detail = library.get_work(works[0].id, session=session)
    assert {tag["name"] for tag in detail["tags"]} == {"Python", "Программирование"}
    assert all(tag["source"] == "meta" for tag in detail["tags"])
    counts = {tag["name"]: tag["count"] for tag in tags.list_tags(session=session)}
    assert counts["Python"] == 1 and counts["Программирование"] == 1
    assert counts["Фэнтези"] == 0 and "Предложенный" not in counts
    snapshots = [w.model_dump() for w in session.exec(select(Work)).all()]
    rows = [r.model_dump() for r in session.exec(select(WorkTag)).all()]
    stats = tags.backfill_tags(session=session, dry_run=True)
    assert (
        stats["books"] == 2 and stats["with_any_tag"] == 2 and stats["with_genre"] == 2
    )
    assert [w.model_dump() for w in session.exec(select(Work)).all()] == snapshots
    assert [r.model_dump() for r in session.exec(select(WorkTag)).all()] == rows
    work_id = works[0].id
    assert library.delete_work(work_id, session=session) == {"ok": True}
    assert session.exec(select(WorkTag).where(WorkTag.work_id == work_id)).all() == []


@pytest.mark.parametrize("merger", ["library", "calibre"])
def test_dedup_preserves_tags_and_tombstones(session, merger):
    from backend.calibre.sync import merge_work

    store.sync_vocab(session)
    keep = Work(title="Дубль", author="Автор")
    drop = Work(title="Дубль", author="Автор")
    session.add_all([keep, drop])
    session.flush()
    keep_id, drop_id = keep.id, drop.id
    python_id = _tag(session, "Python").id
    programming_id = _tag(session, "Программирование").id
    session.add_all(
        [
            WorkTag(work_id=keep_id, tag_id=python_id),
            WorkTag(work_id=drop_id, tag_id=python_id, source="manual", removed=True),
            WorkTag(
                work_id=drop_id, tag_id=programming_id, source="desc", confidence=0.8
            ),
        ]
    )
    session.commit()
    if merger == "library":
        assert library._dedup_works(session) == 1
    else:
        merge_work(session, keep, drop)
        session.commit()
    assert session.get(Work, drop_id) is None
    assert session.get(WorkTag, (keep_id, python_id)).removed
    assert session.get(WorkTag, (keep_id, python_id)).source == "manual"
    assert session.get(WorkTag, (keep_id, programming_id)).source == "desc"
    keep.genres = "Python"
    store.apply_meta_tags(session, keep)
    session.commit()
    assert session.get(WorkTag, (keep_id, python_id)).removed
    assert session.get(WorkTag, (keep_id, programming_id)).source == "desc"
