"""Шаг описания и контракт API #3285; шлюз всегда подменён, без сети."""

import json
import logging
import threading

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.app.db.models import Tag, Work, WorkTag
from backend.app.db.session import get_session
from backend.app.routers import library, tags
from backend.app.tagging import desc, store


@pytest.fixture(autouse=True)
def no_gateway(monkeypatch):
    original = desc.infer_description

    def forbidden(*args, **kwargs):
        raise AssertionError("real gateway must not be called")

    monkeypatch.setattr(desc, "infer_description", forbidden)
    return original


@pytest.fixture
def catalog(session):
    store.sync_vocab(session)
    session.commit()
    return session


def tag(session, name):
    return session.exec(select(Tag).where(Tag.name == name)).one()


def book(session, **kwargs):
    work = Work(**kwargs)
    session.add(work)
    session.flush()
    return work


def link(session, work, name, **kwargs):
    row = WorkTag(work_id=work.id, tag_id=tag(session, name).id, **kwargs)
    session.add(row)
    session.flush()
    return row


def result(names=(), proposals=(), status=""):
    return desc.DescResult(tags=list(names), new_tags=list(proposals), status=status)


def proposal(session, name="Новый жанр"):
    value = Tag(name=name, group="Жанр", state="proposed")
    session.add(value)
    session.flush()
    return value


def test_candidates_include_title_only_and_use_visible_active_tags(catalog):
    s = catalog
    bare = book(s, title="Известная книга", author="Автор")
    hidden = book(s, hidden=True)
    done = book(s, tag_step=2)
    sparse = book(s, tag_step=1)
    rich = book(s, tag_step=1)
    no_genre = book(s, tag_step=1)
    removed = book(s, tag_step=1)
    only_proposed = book(s, tag_step=1)
    for work in (rich, removed):
        for name in ("Python", "Программирование", "Фэнтези", "18+"):
            link(s, work, name, removed=(work is removed and name == "18+"))
    # Четыре тега, но ни одного жанра/темы из целевых групп.
    other_names = [
        t.name for t in s.exec(select(Tag)).all() if t.group not in desc.GENRE_GROUPS
    ][:4]
    for name in other_names:
        link(s, no_genre, name)
    link(s, sparse, "Python")
    for i in range(4):
        p = proposal(s, f"Предложение {i}")
        s.add(WorkTag(work_id=only_proposed.id, tag_id=p.id, source="desc"))
    s.flush()
    ids = set(s.exec(desc.candidate_query()).all())
    assert ids == {bare.id, sparse.id, no_genre.id, removed.id, only_proposed.id}
    assert hidden.id not in ids and done.id not in ids and rich.id not in ids
    assert desc.queued_count(s) == 5


def test_apply_known_normalized_synonyms_unknown_and_refresh(
    catalog, caplog, monkeypatch
):
    s = catalog
    work = book(s)
    link(s, work, "Фэнтези", source="desc")
    link(s, work, "18+", source="manual")
    link(s, work, "Фантастика", source="meta")
    tombstone = link(s, work, "Python", source="desc", removed=True)
    monkeypatch.setattr(desc.log, "handlers", [caplog.handler])
    caplog.set_level(logging.DEBUG, logger=desc.log.name)
    applied = desc.apply_desc_tags(
        s,
        work,
        result(["«ПРОГРАММИРОВАНИЕ»", "Python", "sf_social", "Модель выдумала"]),
    )
    s.commit()
    assert applied == {"unknown_tags": 1, "proposed": 0}
    assert "Модель выдумала" in caplog.text
    rows = {s.get(Tag, r.tag_id).name: r for r in s.exec(select(WorkTag)).all()}
    assert set(rows) == {
        "18+",
        "Фантастика",
        "Python",
        "Программирование",
        "Социальная фантастика",
    }
    assert rows["18+"].source == "manual" and rows["Фантастика"].source == "meta"
    assert tombstone.removed
    assert rows["Программирование"].source == "desc"
    assert rows["Программирование"].confidence == 0.8
    assert work.tag_step == 2 and work.tagged_at
    desc.apply_desc_tags(s, work, result(["Python"]))
    s.commit()
    assert {r.tag_id for r in s.exec(select(WorkTag)).all()} == {
        tag(s, name).id for name in ("18+", "Фантастика", "Python")
    }


@pytest.mark.parametrize(
    ("current", "source", "expected", "expected_source"),
    [
        ("", "", "done", "desc"),
        ("wip", "meta", "wip", "meta"),
        ("", "manual", "", "manual"),
        ("dead", "manual", "dead", "manual"),
    ],
)
def test_status_only_fills_empty_nonmanual(
    catalog, current, source, expected, expected_source
):
    work = book(catalog, write_status=current, write_status_source=source)
    desc.apply_desc_tags(catalog, work, result(status="done"))
    assert (work.write_status, work.write_status_source) == (expected, expected_source)
    assert bool(work.write_status_checked_at) == (expected_source == "desc")


def test_proposed_hidden_normalized_reuse_rejected_and_merged(catalog):
    s = catalog
    work = book(s)
    rejected = Tag(name="Не нужен", group="Жанр", state="rejected")
    merged = Tag(
        name="Старое имя", group="Жанр", state="merged", merged_into=tag(s, "Python").id
    )
    s.add_all([rejected, merged])
    s.flush()
    desc.apply_desc_tags(
        s,
        work,
        result(
            proposals=[
                {"name": "Ёлочный жанр", "group": "Жанр"},
                {"name": " «НЕ НУЖЕН» ", "group": "Жанр"},
            ]
        ),
    )
    s.commit()
    new = tag(s, "Ёлочный жанр")
    assert new.state == "proposed"
    assert s.get(WorkTag, (work.id, new.id)) is not None
    assert s.get(WorkTag, (work.id, rejected.id)) is None
    assert library.get_work(work.id, s)["tags"] == []
    assert library.list_works(session=s)[0]["tag_ids"] == []
    assert new.id not in {row["id"] for row in tags.list_tags(s)}
    desc.apply_desc_tags(
        s,
        work,
        result(
            ["Не нужен"],
            proposals=[
                {"name": " елочный ЖАНР ", "group": "Жанр"},
                {"name": "старое имя", "group": "Жанр"},
            ],
        ),
    )
    s.commit()
    assert len(s.exec(select(Tag).where(Tag.state == "proposed")).all()) == 1
    assert s.get(WorkTag, (work.id, tag(s, "Python").id)).source == "desc"
    assert s.get(WorkTag, (work.id, merged.id)) is None
    assert rejected.state == "rejected" and merged.state == "merged"


def test_unknown_proposal_group_does_not_create_tag(catalog):
    work = book(catalog)
    desc.apply_desc_tags(
        catalog, work, result(proposals=[{"name": "Другой", "group": "Выдуманная"}])
    )
    assert catalog.exec(select(Tag).where(Tag.name == "Другой")).first() is None
    assert work.tag_step == 2


def test_approve_merge_reject_and_synonyms_survive_sync(catalog):
    s = catalog
    works = [book(s, title=f"Книга {i}", hidden=i == 7) for i in range(8)]
    p = proposal(s)
    for work in works:
        s.add(WorkTag(work_id=work.id, tag_id=p.id, source="desc", confidence=0.8))
    approved = proposal(s, "Одобрить")
    rejected = proposal(s, "Отклонить")
    s.add(WorkTag(work_id=works[0].id, tag_id=approved.id, source="desc"))
    s.add(WorkTag(work_id=works[0].id, tag_id=rejected.id, source="desc"))
    link(s, works[0], "Python", source="manual", removed=True)
    s.commit()
    listed = tags.list_proposals(s)
    assert listed[0] == {
        "id": p.id,
        "name": p.name,
        "group": "Жанр",
        "count": 7,
        "books": [{"id": w.id, "title": w.title} for w in works[:5]],
    }
    approval = tags.approve_proposal(approved.id, s)
    assert approval == {
        "ok": True,
        "tag": {"id": approved.id, "name": "Одобрить", "group": "Жанр", "count": 1},
    }
    assert approved.id in {r["id"] for r in library.get_work(works[0].id, s)["tags"]}
    python = tag(s, "Python")
    assert tags.merge_proposal(p.id, tags.MergeTagIn(into_tag_id=python.id), s) == {
        "ok": True
    }
    assert p.state == "merged" and p.merged_into == python.id
    assert p.name in json.loads(python.synonyms)
    assert s.get(WorkTag, (works[0].id, python.id)).removed
    assert s.get(WorkTag, (works[0].id, python.id)).source == "manual"
    assert s.get(WorkTag, (works[1].id, python.id)).confidence == 0.8
    assert not s.exec(select(WorkTag).where(WorkTag.tag_id == p.id)).all()
    assert tags.reject_proposal(rejected.id, s) == {"ok": True}
    assert rejected.state == "rejected"
    assert not s.exec(select(WorkTag).where(WorkTag.tag_id == rejected.id)).all()
    store.sync_vocab(s)
    assert p.name in json.loads(python.synonyms)
    new_work = book(s)
    desc.apply_desc_tags(s, new_work, result([p.name]))
    s.flush()
    assert s.get(WorkTag, (new_work.id, python.id)) is not None
    with pytest.raises(HTTPException) as exc:
        tags.merge_proposal(rejected.id, tags.MergeTagIn(into_tag_id=python.id), s)
    assert exc.value.status_code == 404


def test_manual_add_remove_and_status(catalog):
    s = catalog
    work = book(s)
    python = tag(s, "Python")
    assert library.remove_work_tag(work.id, python.id, s) == []
    row = s.get(WorkTag, (work.id, python.id))
    assert row.source == "manual" and row.removed
    expected = [
        {
            "id": python.id,
            "name": python.name,
            "group": python.group,
            "source": "manual",
        }
    ]
    assert (
        library.add_work_tag(work.id, library.AddTagIn(tag_id=python.id), s) == expected
    )
    assert not row.removed and row.confidence == 1
    desc.apply_desc_tags(s, work, result())
    s.commit()
    assert store.visible_work_tags(s, work.id) == expected
    assert library.remove_work_tag(work.id, python.id, s) == []
    assert s.get(WorkTag, (work.id, python.id)) is not None
    for status in ("done", "wip", "dead", ""):
        assert library.set_write_status(
            work.id, library.WriteStatusIn(status=status), s
        ) == {"write_status": status, "write_status_source": "manual"}
        assert library.get_work(work.id, s)["write_status_source"] == "manual"
    desc.apply_desc_tags(s, work, result(["Python"], status="done"))
    assert (
        row.removed and work.write_status == "" and work.write_status_source == "manual"
    )
    with pytest.raises(ValidationError):
        library.WriteStatusIn(status="unknown")
    with pytest.raises(HTTPException):
        library.add_work_tag(999999, library.AddTagIn(tag_id=python.id), s)
    with pytest.raises(HTTPException):
        library.add_work_tag(work.id, library.AddTagIn(tag_id=proposal(s).id), s)


def test_progress_counts(catalog):
    s = catalog
    works = [book(s, tag_step=i) for i in range(5)]
    book(s, hidden=True, tag_step=2)
    link(s, works[0], "Python")
    link(s, works[1], "Python", removed=True)
    p = proposal(s)
    s.add(WorkTag(work_id=works[2].id, tag_id=p.id, source="desc"))
    s.flush()
    assert tags.tagging_progress(s) == {
        "total": 5,
        "tagged": 1,
        "by_step": {"meta": 1, "desc": 1, "search": 1, "read": 1},
        "queued": 2,
        "pending_proposals": 1,
        "running": False,
    }


def test_batch_errors_continue_empty_success_limit_and_delay(
    catalog, monkeypatch, caplog
):
    s = catalog
    works = [book(s, title=str(i)) for i in range(4)]
    s.commit()
    monkeypatch.setattr(desc.db, "engine", s.get_bind())
    calls, delays = [], []

    def infer(messages):
        title = json.loads(messages[1]["content"])["title"]
        calls.append(title)
        if title == "0":
            raise httpx.ConnectError("gateway down")
        if title == "1":
            raise ValueError("invalid JSON")
        return result(["Hallucination"])

    monkeypatch.setattr(desc, "infer_description", infer)
    monkeypatch.setattr(desc.time, "sleep", delays.append)
    monkeypatch.setattr(desc.log, "handlers", [caplog.handler])
    caplog.set_level(logging.WARNING)
    stats = desc.run_desc_batch(limit=3)
    s.expire_all()
    assert stats == {
        "started": True,
        "processed": 1,
        "errors": 2,
        "unknown_tags": 1,
        "proposed": 0,
    }
    assert [w.tag_step for w in works] == [0, 0, 2, 0]
    assert works[2].tagged_at
    assert calls == ["0", "1", "2"] and delays == [2, 2]
    assert "gateway down" in caplog.text and "invalid JSON" in caplog.text
    assert all(str(w.id) in caplog.text for w in works[:2])
    assert not desc.is_running()


def test_batch_rereads_manual_changes(catalog, monkeypatch):
    s = catalog
    work = book(s)
    s.commit()
    monkeypatch.setattr(desc.db, "engine", s.get_bind())

    def infer(messages):
        library.remove_work_tag(work.id, tag(s, "Python").id, s)
        library.set_write_status(work.id, library.WriteStatusIn(status=""), s)
        return result(["Python"], status="done")

    monkeypatch.setattr(desc, "infer_description", infer)
    assert desc.run_desc_batch(1)["processed"] == 1
    s.expire_all()
    assert s.get(WorkTag, (work.id, tag(s, "Python").id)).removed
    assert work.write_status == "" and work.write_status_source == "manual"


def test_second_run_does_not_start_and_running_covers_thread_start(
    catalog, monkeypatch
):
    threads = []

    class DeferredThread:
        def __init__(self, **kwargs):
            threads.append(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(desc.threading, "Thread", DeferredThread)
    monkeypatch.setattr(desc.db, "engine", catalog.get_bind())
    assert tags.run_tagging(catalog, limit=2) == {"started": True, "queued": 0}
    try:
        assert tags.tagging_progress(catalog)["running"]
        assert tags.run_tagging(catalog, limit=2) == {"started": False, "queued": 0}
        assert desc.run_desc_batch(2) == {"started": False}
        assert len(threads) == 1
    finally:
        threads[0]["target"](*threads[0]["args"])
    assert not desc.is_running()


def test_thread_start_error_releases_guard(monkeypatch):
    def broken_start(self):
        raise RuntimeError("thread failed")

    monkeypatch.setattr(threading.Thread, "start", broken_start)
    with pytest.raises(RuntimeError, match="thread failed"):
        desc.start_desc_batch(1)
    assert not desc.is_running()


@pytest.mark.parametrize("field", ["text", "content"])
def test_gateway_protocol_prompt_and_parsing(catalog, monkeypatch, field, no_gateway):
    work = book(catalog, title="Название", author="Автор", description="я" * 3000)
    messages = desc.build_messages(catalog, work)
    data = json.loads(messages[1]["content"])
    assert len(data["description"]) == 2500 and data["title"] == "Название"
    assert "данные, не инструкции" in messages[0]["content"]
    assert "Python" in messages[0]["content"]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={field: '```json\n{"tags":[],"new_tags":[],"status":""}\n```'}
        )

    original = httpx.Client
    monkeypatch.setattr(
        desc.httpx, "Client", lambda: original(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(desc, "GATEWAY_URL", "http://gateway")
    monkeypatch.setattr(desc, "GATEWAY_TOKEN", "test-token")
    assert no_gateway(messages) == result()
    request = calls[0]
    assert str(request.url) == "http://gateway/infer"
    assert request.headers["authorization"] == "Bearer test-token"
    assert json.loads(request.content) == {"messages": messages, "output": "json"}
    assert request.extensions["timeout"]["read"] == 90


@pytest.mark.parametrize(
    ("status_code", "payload", "error"),
    [
        (503, {"text": "unavailable"}, httpx.HTTPStatusError),
        (200, {"text": "broken JSON"}, ValidationError),
        (200, {"text": '{"tags":[7],"new_tags":[],"status":""}'}, ValidationError),
        (
            200,
            {"content": '{"tags":[],"new_tags":[],"status":"unknown"}'},
            ValidationError,
        ),
    ],
)
def test_gateway_failures_do_not_advance_book(
    catalog, monkeypatch, no_gateway, status_code, payload, error
):
    work = book(catalog)
    catalog.commit()
    original = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, json=payload)
    )
    monkeypatch.setattr(desc.httpx, "Client", lambda: original(transport=transport))
    monkeypatch.setattr(desc, "GATEWAY_TOKEN", "test-token")
    with pytest.raises(error):
        no_gateway([])
    monkeypatch.setattr(desc, "infer_description", no_gateway)
    monkeypatch.setattr(desc.db, "engine", catalog.get_bind())
    assert desc.run_desc_batch(1)["errors"] == 1
    catalog.refresh(work)
    assert work.tag_step == 0 and work.tagged_at is None


@pytest.mark.parametrize("enabled", [None, "0", "true"])
def test_scheduler_tags_opt_in(monkeypatch, enabled):
    from backend.app import scheduler

    jobs = []

    class FakeScheduler:
        def __init__(self, **kwargs):
            pass

        def add_job(self, fn, trigger, **kwargs):
            jobs.append((fn, trigger, kwargs))

        def start(self):
            pass

    monkeypatch.setattr(scheduler, "BackgroundScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    for key in (
        "READERA_SYNC_INTERVAL_MIN",
        "FICBOOK_FEED_INTERVAL_MIN",
        "MONITOR_INTERVAL_MIN",
        "CALIBRE_SYNC_INTERVAL_MIN",
    ):
        monkeypatch.setattr(scheduler, key, 0)
    if enabled is None:
        monkeypatch.delenv("READER_TAGS_AUTORUN", raising=False)
    else:
        monkeypatch.setenv("READER_TAGS_AUTORUN", enabled)
    scheduler.start()
    assert len(jobs) == (1 if enabled == "true" else 0)
    if jobs:
        fn, trigger, options = jobs[0]
        assert trigger == "interval" and options["minutes"] == 30
        assert options["max_instances"] == 1 and options["id"] == "tags_desc"
        calls = []
        monkeypatch.setattr(desc, "run_desc_batch", lambda limit: calls.append(limit))
        fn()
        assert calls == [40]


@pytest.fixture
def desc_api():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(library.router)
    app.include_router(tags.router)
    with Session(engine) as session:
        store.sync_vocab(session)
        session.commit()
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield client, session
    engine.dispose()


def test_http_contract(desc_api, monkeypatch):
    client, s = desc_api
    work = book(s)
    python = tag(s, "Python")
    s.commit()
    assert (
        client.post(f"/api/library/{work.id}/tags", json={"tag_id": python.id}).json()[
            0
        ]["source"]
        == "manual"
    )
    assert client.delete(f"/api/library/{work.id}/tags/{python.id}").json() == []
    assert client.put(
        f"/api/library/{work.id}/write-status", json={"status": "done"}
    ).json() == {"write_status": "done", "write_status_source": "manual"}
    assert (
        client.put(
            f"/api/library/{work.id}/write-status", json={"status": "bad"}
        ).status_code
        == 422
    )
    assert (
        client.get(f"/api/library/{work.id}").json()["write_status_source"] == "manual"
    )
    assert client.get("/api/tags/progress").json()["queued"] == 1
    monkeypatch.setattr(desc, "start_desc_batch", lambda limit: True)
    assert client.post("/api/tags/run?step=2&limit=1").json() == {
        "started": True,
        "queued": 1,
    }
    assert client.post("/api/tags/run?step=3").status_code == 422
    assert client.post("/api/tags/run?limit=0").status_code == 422
    proposed = [proposal(s, f"HTTP предложение {i}") for i in range(3)]
    for p in proposed:
        s.add(WorkTag(work_id=work.id, tag_id=p.id, source="desc"))
    s.commit()
    assert len(client.get("/api/tags/proposals").json()) == 3
    assert (
        client.post(f"/api/tags/proposals/{proposed[0].id}/approve").json()["tag"][
            "count"
        ]
        == 1
    )
    assert client.post(
        f"/api/tags/proposals/{proposed[1].id}/merge", json={"into_tag_id": python.id}
    ).json() == {"ok": True}
    assert client.post(f"/api/tags/proposals/{proposed[2].id}/reject").json() == {
        "ok": True
    }
    assert client.get("/api/tags/proposals").json() == []
