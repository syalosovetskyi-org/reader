"""Скрипт чистки истории (serg/tasks#3239): классы строк, транзакция, повтор = 0 изменений."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text

SPEC = importlib.util.spec_from_file_location(
    "cleanup_history_3239", Path(__file__).resolve().parents[1] / "scripts" / "cleanup_history_3239.py")
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup  # dataclass ищет модуль в sys.modules
SPEC.loader.exec_module(cleanup)

T0 = datetime(2026, 11, 1, 12, 0, 0)


def _db(tmp_path):
    url = f"sqlite:///{tmp_path / 'h.db'}"
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE positionhistory (id INTEGER PRIMARY KEY, work_id INTEGER, ratio FLOAT, "
            "locator VARCHAR, text_anchor VARCHAR, chapter VARCHAR, reason VARCHAR, device VARCHAR, "
            "book_rev VARCHAR, created_at DATETIME)"))
        rows = [
            # id, work, locator, reason, device, +sec
            (1, 1, "a", "toc", "", 0),            # клиентская — оставить
            (2, 1, "a", "overwrite", "windows", 1),  # дубль клиентской через 900 мс — удалить
            (3, 1, "b", "overwrite", "windows", 60),  # граница главы — удалить
            (4, 1, "c", "overwrite", "android", 120),  # смена windows→android — device
            (5, 1, "d", "overwrite", "android", 180),  # тот же android — удалить
            (6, 1, "e", "overwrite", "server", 240),   # докачка — удалить
            (7, 2, "x", "overwrite", "linux", 0),      # первая строка книги — удалить
            (8, 2, "y", "link", "", 10),              # клиентская — оставить
        ]
        for rid, wid, loc, reason, dev, sec in rows:
            c.execute(text(
                "INSERT INTO positionhistory VALUES (:id, :w, .1, :loc, '', '', :r, :d, '', :t)"),
                {"id": rid, "w": wid, "loc": loc, "r": reason, "d": dev, "t": T0 + timedelta(seconds=sec)})
    return url, engine


def test_plan_classes_and_idempotent_apply(tmp_path, capsys):
    url, engine = _db(tmp_path)
    assert cleanup.main(["--db-url", url]) == 0  # dry-run
    import json
    dry = json.loads(capsys.readouterr().out)
    assert dry == {"mode": "dry-run", "delete_server": 1, "delete_client_duplicate": 1,
                   "delete_other_overwrite": 3, "rename_to_device": 1, "kept_client_rows": 2,
                   "delete_total": 5}
    with engine.connect() as c:  # dry-run ничего не меняет
        assert c.execute(text("SELECT count(*) FROM positionhistory")).scalar_one() == 8

    # Расхождение с dry-run — откат, таблица не тронута, копии нет.
    assert cleanup.main(["--db-url", url, "--apply", "--expect-delete", "4"]) == 1
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM positionhistory")).scalar_one() == 8
        assert not c.execute(text("SELECT name FROM sqlite_master WHERE name='positionhistory_bak_3239'")).all()

    assert cleanup.main(["--db-url", url, "--apply", "--expect-delete", "5"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (out["applied"], out["deleted"], out["renamed"]) == (True, 5, 1)
    with engine.connect() as c:
        left = c.execute(text("SELECT id, reason, device FROM positionhistory ORDER BY id")).all()
        assert [tuple(r) for r in left] == [(1, "toc", ""), (4, "device", "windows"), (8, "link", "")]
        assert c.execute(text("SELECT count(*) FROM positionhistory_bak_3239")).scalar_one() == 8

    # Повторный запуск: ноль изменений.
    assert cleanup.main(["--db-url", url, "--apply"]) == 0
    again = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert again["applied"] is False and again["delete_total"] == 0 and again["rename_to_device"] == 0
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM positionhistory")).scalar_one() == 3
