"""Чистка истории переходов от строк, которые наплодил старый сервер (serg/tasks#3239).

До #3239 сервер сам угадывал «затирание» по разнице доли или смене главы и
писал строку `overwrite` на каждой границе главы, дубль клиентской записи
через 900 мс и строки `device='server'` при докачке и слиянии дублей. История
упиралась в MAX_HISTORY=50 и состояла из «затёрто другой вкладкой».

Классы строк:
  * device='server'                       — удалить;
  * overwrite-дубль клиентской записи     — удалить (то же место, что у
    клиентской строки той же книги, записанной не раньше чем за DUP_WINDOW);
  * overwrite при смене семейства UA      — оставить, reason='device',
    device = семейство того, кто ЧИТАЛ место (писал предыдущим);
  * остальные overwrite                   — удалить;
  * клиентские строки (toc, link, …)      — не трогать.

Запуск (по умолчанию — dry-run, только SELECT; init_db не вызывается):
    READER_DB_URL=... python scripts/cleanup_history_3239.py            # отчёт
    READER_DB_URL=... python scripts/cleanup_history_3239.py --apply --expect-delete N

--apply в одной транзакции: копия таблицы в positionhistory_bak_3239, удаление,
переименование; число удалённых строк сверяется с планом (и с --expect-delete
из dry-run), расхождение — откат. Повторный запуск ничего не меняет.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

DUP_WINDOW = timedelta(seconds=30)
FAMILIES = {"android", "windows", "ios", "macos", "linux"}
BACKUP = "positionhistory_bak_3239"


@dataclass
class Plan:
    delete_server: list[int] = field(default_factory=list)
    delete_dup: list[int] = field(default_factory=list)
    delete_other: list[int] = field(default_factory=list)
    rename: dict[int, str] = field(default_factory=dict)  # id -> device (кто читал)
    kept_client: int = 0

    @property
    def delete_ids(self) -> list[int]:
        return sorted(self.delete_server + self.delete_dup + self.delete_other)

    def summary(self) -> dict:
        return {
            "delete_server": len(self.delete_server),
            "delete_client_duplicate": len(self.delete_dup),
            "delete_other_overwrite": len(self.delete_other),
            "rename_to_device": len(self.rename),
            "kept_client_rows": self.kept_client,
            "delete_total": len(self.delete_ids),
        }


def _ts(v) -> datetime:
    if isinstance(v, datetime):
        return v
    # SQLite отдаёт строку.
    return datetime.fromisoformat(str(v))


def _table(conn: Connection, schema: str) -> str:
    return f'"{schema}"."positionhistory"' if conn.dialect.name == "postgresql" and schema else "positionhistory"


def build_plan(conn: Connection, schema: str = "reader") -> Plan:
    """Только SELECT: разложить строки по классам."""
    rows = conn.execute(text(
        f"SELECT id, work_id, locator, text_anchor, reason, device, created_at "
        f"FROM {_table(conn, schema)} ORDER BY work_id, created_at, id"
    )).mappings().all()
    plan = Plan()
    by_work: dict[int, list] = defaultdict(list)
    for r in rows:
        by_work[r["work_id"]].append(r)
    for work_rows in by_work.values():
        client = [r for r in work_rows if r["reason"] != "overwrite" and r["device"] != "server"]
        plan.kept_client += len(client)
        prev_family = None
        for r in work_rows:
            if r["device"] == "server":
                plan.delete_server.append(r["id"])
                continue
            if r["reason"] != "overwrite":
                continue
            fam = (r["device"] or "").lower()
            t = _ts(r["created_at"])
            dup = any(
                c["locator"] == r["locator"] and c["text_anchor"] == r["text_anchor"]
                and timedelta(0) <= t - _ts(c["created_at"]) <= DUP_WINDOW
                for c in client
            )
            if dup:
                plan.delete_dup.append(r["id"])
            elif fam in FAMILIES and prev_family in FAMILIES and fam != prev_family:
                # Место затёрло другое устройство: там читали на prev_family.
                plan.rename[r["id"]] = prev_family
            else:
                plan.delete_other.append(r["id"])
            if fam in FAMILIES:
                prev_family = fam
    return plan


def apply_plan(conn: Connection, schema: str = "reader", expect_delete: int | None = None) -> dict:
    """Выполнить план в ТЕКУЩЕЙ транзакции conn. Расхождение — исключение (откат)."""
    table = _table(conn, schema)
    pg = conn.dialect.name == "postgresql"
    if pg:
        # Пока чистим, читалка не должна дописать строки, которых нет в плане.
        conn.execute(text(f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE"))
    plan = build_plan(conn, schema)
    summary = plan.summary()
    if expect_delete is not None and expect_delete != len(plan.delete_ids):
        raise RuntimeError(f"план разошёлся с dry-run: удалить {len(plan.delete_ids)}, ожидалось {expect_delete}")
    if not plan.delete_ids and not plan.rename:
        return {**summary, "applied": False, "reason": "нечего менять"}
    backup = f'"{schema}"."{BACKUP}"' if pg and schema else BACKUP
    if pg:
        conn.execute(text(f"CREATE TABLE {backup} AS TABLE {table}"))
    else:
        conn.execute(text(f"CREATE TABLE {backup} AS SELECT * FROM {table}"))
    deleted = 0
    ids = plan.delete_ids
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        params = {f"i{k}": v for k, v in enumerate(chunk)}
        marks = ", ".join(f":i{k}" for k in range(len(chunk)))
        deleted += conn.execute(text(f"DELETE FROM {table} WHERE id IN ({marks})"), params).rowcount
    if deleted != len(ids):
        raise RuntimeError(f"удалено {deleted} строк, по плану {len(ids)} — откат")
    renamed = 0
    for rid, dev in plan.rename.items():
        renamed += conn.execute(
            text(f"UPDATE {table} SET reason = 'device', device = :dev WHERE id = :id AND reason = 'overwrite'"),
            {"dev": dev, "id": rid},
        ).rowcount
    if renamed != len(plan.rename):
        raise RuntimeError(f"переименовано {renamed} строк, по плану {len(plan.rename)} — откат")
    return {**summary, "applied": True, "deleted": deleted, "renamed": renamed, "backup": backup}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="только отчёт (по умолчанию)")
    mode.add_argument("--apply", action="store_true", help="выполнить в одной транзакции")
    ap.add_argument("--db-url", default=os.getenv("READER_DB_URL", ""), help="по умолчанию READER_DB_URL")
    ap.add_argument("--schema", default="reader", help="схема Postgres (для SQLite не используется)")
    ap.add_argument("--expect-delete", type=int, default=None, help="число delete_total из dry-run")
    args = ap.parse_args(argv)
    if not args.db_url:
        print("нужен --db-url или READER_DB_URL", file=sys.stderr)
        return 2
    engine = create_engine(args.db_url)
    if not args.apply:
        with engine.connect() as conn:
            plan = build_plan(conn, args.schema)
            conn.rollback()
        print(json.dumps({"mode": "dry-run", **plan.summary()}, ensure_ascii=False))
        return 0
    try:
        with engine.begin() as conn:
            result = apply_plan(conn, args.schema, args.expect_delete)
    except (RuntimeError, SQLAlchemyError) as e:
        # engine.begin() уже откатил транзакцию; причина — в stderr и коде выхода.
        print(f"откат: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"mode": "apply", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
