"""Сбой проверки зеркала author.today не должен быть неотличим от «на зеркале
новых глав нет» (serg/tasks#3045).

Раньше `except Exception: pass` в check_one и `return None` в _at_task глотали
протухшую сессию AT, InvalidToken при расшифровке кредов и смену разметки без
следа: книга молча переставала подтягиваться с более полного зеркала.
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from backend.accounts import monitor
from backend.app.db.models import Monitored, Work

SRC = "https://ficbook.net/readfic/123"


def _boom(*_a, **_k):
    raise RuntimeError("AT session expired")


def _setup(session: Session, monkeypatch):
    w = Work(title="Книга", author="Автор", file_path="/nonexistent.epub",
             file_format="epub", source_url=SRC)
    session.add(w)
    session.commit()
    session.refresh(w)
    session.add(Monitored(source_url=SRC, work_id=w.id, last_seen_chapters=10))
    session.commit()
    monkeypatch.setattr(monitor, "_chapter_count", lambda *a, **k: 10)
    monkeypatch.setattr(monitor, "_mirror_eligible", lambda *a: True)
    monkeypatch.setattr(monitor, "_file_chapters", lambda w_: 10)
    monkeypatch.setattr(monitor.store, "creds_for_host", lambda s, h: None)
    monkeypatch.setattr(monitor, "_check_mirrors", _boom)
    return w


def test_check_one_mirror_failure_is_logged_and_reported(session, monkeypatch, caplog):
    w = _setup(session, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="reader.monitor"):
        res = monitor.check_one(session, w.id, auto_download=False)
    assert "AT session expired" in res.get("mirror_error", "")
    assert any("mirror check failed" in r.getMessage() for r in caplog.records)


def test_at_task_mirror_failure_is_logged(monkeypatch, caplog):
    monitor.reset_mirror_cache()
    monkeypatch.setattr(monitor, "_mirror_eligible", lambda *a: True)
    monkeypatch.setattr(monitor, "_check_mirrors", _boom)
    task = {"host": "ficbook.net", "work_id": 77, "title": "Книга", "our": {}}
    with caplog.at_level(logging.WARNING, logger="reader.monitor"):
        assert monitor._at_task(task) is None
    assert any("mirror check failed" in r.getMessage() for r in caplog.records)
