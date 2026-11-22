"""Издания одного произведения (serg/tasks#3240): API группы, язык, перенос места.

Проверяет:
- связать / отвязать / слить группы; группа из одного издания снимается;
- список изданий с прогрессом каждого, «последнее» издание;
- подсказки «похоже на то же произведение» и поиск;
- переключатель sync и позицию группы (sync-target) — с учётом own_saved_at;
- перенос по главе и доле (RU ↔ EN, номер «Глава 5» ↔ «Chapter 5»), по якорю
  для того же текста, по доле для PDF;
- язык из opf / fb2 / текста и ручную правку;
- дедуп не трогает связанные издания; удаление книги из группы.
"""

from __future__ import annotations

import io
import os
import uuid
import zipfile
from pathlib import Path

import pytest
from sqlmodel import Session, select

from backend.app import editions as ed
from backend.app.db.models import EditionGroup, Progress, Work
from backend.app.db.session import engine

BOOKS = Path(os.environ["READER_DATA_DIR"]) / "editions-tests"


def epub(path: Path, chapters: list[str], lang: str | None, front: int = 0, para: str = "Текст") -> Path:
    """EPUB c front служебными секциями и главами chapters (заголовки в nav)."""
    buf = io.BytesIO()
    names = [f"f{i}.xhtml" for i in range(front)] + [f"c{i}.xhtml" for i in range(len(chapters))]
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        lang_xml = f"<dc:language>{lang}</dc:language>" if lang else ""
        z.writestr("book.opf", '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>' + lang_xml + '</metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' + "".join(f'<item id="i{k}" href="{n}" media-type="application/xhtml+xml"/>' for k, n in enumerate(names)) + "</manifest><spine>" + "".join(f'<itemref idref="i{k}"/>' for k in range(len(names))) + "</spine></package>")
        z.writestr("nav.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>' + "".join(f'<li><a href="f{i}.xhtml">Титул {i}</a></li>' for i in range(front)) + "".join(f'<li><a href="c{i}.xhtml">{t}</a></li>' for i, t in enumerate(chapters)) + "</ol></nav></body></html>")
        for n in names:
            z.writestr(n, f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{(para + " ") * 400}</p></body></html>')
    BOOKS.mkdir(exist_ok=True)
    path.write_bytes(buf.getvalue())
    return path


def add_work(title="Unsong", author="Scott Alexander", path: Path | None = None, fmt="epub", **kw) -> int:
    with Session(engine) as s:
        w = Work(title=title, author=author, site="upload", file_path=str(path or ""),
                 file_format=fmt, sha1=uuid.uuid4().hex, **kw)
        s.add(w)
        s.commit()
        return w.id


def put_progress(wid: int, **kw) -> None:
    with Session(engine) as s:
        p = s.exec(select(Progress).where(Progress.work_id == wid)).first() or Progress(work_id=wid)
        for k, v in kw.items():
            setattr(p, k, v)
        s.add(p)
        s.commit()


def tmp(name: str) -> Path:
    BOOKS.mkdir(exist_ok=True)
    return BOOKS / f"{uuid.uuid4().hex}-{name}"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for p in BOOKS.glob("*"):
        p.unlink(missing_ok=True)


RU = [f"Глава {i}" for i in range(1, 9)]
EN = [f"Chapter {i}" for i in range(1, 9)]


def test_link_list_unlink(client):
    a = add_work(path=epub(tmp("ru.epub"), RU, "ru"))
    b = add_work(title="Unsong", path=epub(tmp("en.epub"), EN, "en"))
    r = client.get(f"/api/editions/{a}")
    assert r.status_code == 200 and r.json()["group"] is None
    assert [e["id"] for e in r.json()["editions"]] == [a]

    r = client.post(f"/api/editions/{a}/link", json={"other_id": b})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["group"]["sync"] is True
    assert {e["id"] for e in d["editions"]} == {a, b}
    langs = {e["id"]: e["language"] for e in d["editions"]}
    assert langs == {a: "ru", b: "en"}

    put_progress(a, ratio=0.4, chapter="Глава 4", saved_at=1000)
    put_progress(b, ratio=0.7, chapter="Chapter 6", saved_at=2000)
    d = client.get(f"/api/editions/{b}").json()
    rows = {e["id"]: e for e in d["editions"]}
    assert rows[a]["ratio"] == pytest.approx(0.4) and rows[a]["chapter"] == "Глава 4"
    assert rows[b]["current"] and not rows[a]["current"]
    assert d["latest_id"] == b

    # Самосвязь — ошибка, повторная связь — без изменений.
    assert client.post(f"/api/editions/{a}/link", json={"other_id": a}).status_code == 400
    assert client.post(f"/api/editions/{b}/link", json={"other_id": a}).status_code == 200

    r = client.delete(f"/api/editions/{a}/link")
    assert r.status_code == 200 and r.json()["group"] is None
    with Session(engine) as s:
        # Группа из одного издания снята целиком, книги и прогресс на месте.
        assert s.get(Work, b).edition_group_id is None
        assert s.exec(select(Progress).where(Progress.work_id == a)).first().ratio == pytest.approx(0.4)
        assert s.get(Work, a) and s.get(Work, b)


def test_merge_groups_and_three_editions(client):
    a, b, c, d = (add_work(title="Unsong") for _ in range(4))
    gid1 = client.post(f"/api/editions/{a}/link", json={"other_id": b}).json()["group"]["id"]
    gid2 = client.post(f"/api/editions/{c}/link", json={"other_id": d}).json()["group"]["id"]
    assert gid1 != gid2
    r = client.post(f"/api/editions/{b}/link", json={"other_id": c}).json()
    assert r["group"]["id"] == gid1 and {e["id"] for e in r["editions"]} == {a, b, c, d}
    with Session(engine) as s:
        assert s.get(EditionGroup, gid2) is None
    # Отвязка одного из четырёх группу не снимает.
    r = client.delete(f"/api/editions/{d}/link").json()
    assert {e["id"] for e in client.get(f"/api/editions/{a}").json()["editions"]} == {a, b, c}


def test_sync_toggle_and_sync_target(client):
    a = add_work(path=epub(tmp("ru.epub"), RU, "ru"))
    b = add_work(path=epub(tmp("en.epub"), EN, "en"))
    assert client.put(f"/api/editions/{a}/sync", json={"sync": False}).status_code == 409
    client.post(f"/api/editions/{a}/link", json={"other_id": b})
    # EN дочитан до 6-й главы (секция 5), середина главы.
    put_progress(b, ratio=0.66, locator="epubcfi(/6/12!/4/2,/1:0,/1:5)", chapter="Chapter 6",
                 text_anchor="Text Text", saved_at=5000)
    put_progress(a, ratio=0.1, saved_at=1000)

    t = client.get(f"/api/editions/{a}/sync-target").json()
    assert t["apply"] is True and t["source_id"] == b
    assert t["method"] == "chapter" and t["same_text"] is False
    assert t["target"]["label"] == "Глава 6" and t["target"]["index"] == 5
    assert 0.0 <= t["target"]["fraction"] <= 1.0

    # Своё свежее — не переносим. own_saved_at перекрывает пересохранение читалкой.
    put_progress(a, saved_at=9000)
    assert client.get(f"/api/editions/{a}/sync-target").json()["reason"] == "own_is_latest"
    assert client.get(f"/api/editions/{a}/sync-target?own_saved_at=1000").json()["apply"] is True

    assert client.put(f"/api/editions/{a}/sync", json={"sync": False}).json()["group"]["sync"] is False
    assert client.get(f"/api/editions/{a}/sync-target?own_saved_at=0").json()["reason"] == "sync_off"


def test_map_by_number_with_front_matter_and_anchor_same_text(client):
    # RU: два титульных листа перед главами; номер главы совпадает по заголовку.
    a = add_work(path=epub(tmp("ru.epub"), RU, "ru", front=2))
    b = add_work(path=epub(tmp("en.epub"), EN, "en"))
    client.post(f"/api/editions/{a}/link", json={"other_id": b})
    put_progress(b, ratio=0.3, locator="epubcfi(/6/6!/4/2)", chapter="Chapter 3", saved_at=10)
    t = client.get(f"/api/editions/{a}/sync-target?own_saved_at=0").json()
    assert t["target"]["label"] == "Глава 3" and t["target"]["index"] == 4

    # Тот же текст (PDF↔EPUB одного перевода): метод — якорь.
    c = add_work(path=epub(tmp("ru2.epub"), RU, "ru"))
    client.post(f"/api/editions/{a}/link", json={"other_id": c})
    put_progress(c, ratio=0.5, locator="epubcfi(/6/10!/4/2)", text_anchor="Текст Текст Текст",
                 chapter="Глава 5", saved_at=20)
    t = client.get(f"/api/editions/{a}/sync-target?own_saved_at=0").json()
    assert t["source_id"] == c and t["same_text"] and t["method"] == "anchor"
    assert t["target"]["label"] == "Глава 5"

    # Разные метки одного языка — разный текст, переносим по главе.
    client.put(f"/api/editions/{a}/meta", json={"edition_label": "перевод Катаева"})
    client.put(f"/api/editions/{c}/meta", json={"edition_label": "перевод Шелли"})
    t = client.get(f"/api/editions/{a}/sync-target?own_saved_at=0").json()
    assert t["same_text"] is False and t["method"] == "chapter"


def test_pdf_maps_by_ratio(client):
    a = add_work(path=epub(tmp("ru.epub"), RU, "ru"))
    pdf = tmp("scan.pdf")
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    b = add_work(path=pdf, fmt="pdf")
    client.post(f"/api/editions/{a}/link", json={"other_id": b})
    put_progress(b, ratio=0.42, locator="epubcfi(/6/20)", file_format="pdf", saved_at=50)
    t = client.get(f"/api/editions/{a}/sync-target?own_saved_at=0").json()
    assert t["method"] == "ratio" and t["ratio"] == pytest.approx(0.42)
    # Открыт сам PDF — тоже только доля.
    put_progress(a, ratio=0.3, locator="epubcfi(/6/4!/4/2)", saved_at=100)
    t = client.get(f"/api/editions/{b}/sync-target?fmt=pdf&own_saved_at=0").json()
    assert t["method"] == "ratio" and t["ratio"] == pytest.approx(0.3)


def test_suggest(client):
    a = add_work(title="Bluestar Wanderer", author="Scott Alexander")
    same = add_work(title="Bluestar Wanderer (перевод)", author="Скотт Александер")
    namesake = add_work(title="Песнь", author="Другой Автор")
    other = add_work(title="Совсем другая книга", author="Кто-то")
    d = client.get(f"/api/editions/{a}/suggest").json()
    sug = [x["id"] for x in d["suggested"]]
    assert same in sug and other not in sug and namesake not in sug
    assert other in [x["id"] for x in d["others"]]
    d = client.get(f"/api/editions/{a}/suggest", params={"q": "другая"}).json()
    assert [x["id"] for x in d["others"]] == [other] and d["suggested"] == []
    # Уже связанные в подсказки не попадают.
    client.post(f"/api/editions/{a}/link", json={"other_id": same})
    d = client.get(f"/api/editions/{a}/suggest").json()
    assert same not in [x["id"] for x in d["suggested"] + d["others"]]


def test_language_detection_and_manual(client):
    a = add_work(path=epub(tmp("x.epub"), EN, "en-US"))
    b = add_work(path=epub(tmp("y.epub"), RU, None, para="Привет мир это русский текст"))
    fb2 = tmp("z.fb2")
    fb2.write_text('<?xml version="1.0" encoding="utf-8"?><FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description><title-info><lang>ru</lang></title-info></description><body><section><title><p>Глава 1</p></title><p>Текст</p></section></body></FictionBook>', encoding="utf-8")
    c = add_work(path=fb2, fmt="fb2")
    client.post(f"/api/editions/{a}/link", json={"other_id": b})
    client.post(f"/api/editions/{a}/link", json={"other_id": c})
    langs = {e["id"]: e["language"] for e in client.get(f"/api/editions/{a}").json()["editions"]}
    assert langs == {a: "en", b: "ru", c: "ru"}
    r = client.put(f"/api/editions/{b}/meta", json={"language": "uk", "edition_label": "оригинал"})
    row = {e["id"]: e for e in r.json()["editions"]}[b]
    assert row["language"] == "uk" and row["language_manual"] and row["edition_label"] == "оригинал"
    assert client.put(f"/api/editions/{b}/meta", json={"language": "клингон"}).status_code == 400
    # Пересканирование метаданных не затирает ручной язык.
    from backend.app import bookmeta
    with Session(engine) as s:
        w = s.get(Work, b)
        bookmeta.apply_meta(w, {"language": "ru"}, overwrite=True)
        assert w.language == "uk"


def test_language_by_text_and_numbers():
    assert ed.language_by_text("Привет, как дела у тебя сегодня? " * 20) == "ru"
    assert ed.language_by_text("Hello, how are you doing today? " * 20) == "en"
    assert ed.language_by_text("Привіт, як справи у тебе сьогодні? Їжак і ґанок. " * 20) == "uk"
    assert ed.language_by_text("ok") == ""
    assert ed.chapter_number("Глава 5. Встреча") == 5
    assert ed.chapter_number("Chapter XII") == 12
    assert ed.chapter_number("7. Night") == 7
    assert ed.chapter_number("Пролог") is None


def test_dedup_keeps_linked_editions(client):
    """Связанные издания дедуп не сливает и не удаляет (serg/tasks#3234, #3240)."""
    from backend.app.routers.library import _dedup_works

    p1, p2 = epub(tmp("a.epub"), RU, "ru"), epub(tmp("b.epub"), RU, "ru")
    # Одинаковые название/автор/формат и не upload — раньше это был дубль.
    a = add_work(title="Дубль-издание", author="Автор Один", path=p1)
    b = add_work(title="Дубль-издание", author="Автор Один", path=p2)
    with Session(engine) as s:
        for wid in (a, b):
            w = s.get(Work, wid)
            w.site = "ficbook"
            s.add(w)
        s.commit()
    client.post(f"/api/editions/{a}/link", json={"other_id": b})
    with Session(engine) as s:
        _dedup_works(s)
    with Session(engine) as s:
        assert s.get(Work, a) and s.get(Work, b)
    assert p1.exists() and p2.exists()


def test_delete_work_from_group(client):
    files = {"file": ("ed-del.epub", b"content-ed-del", "application/epub+zip")}
    a = client.post("/api/library/upload", files=files).json()["id"]
    b = add_work(title="Unsong")
    c = add_work(title="Unsong")
    client.post(f"/api/editions/{a}/link", json={"other_id": b})
    gid = client.post(f"/api/editions/{a}/link", json={"other_id": c}).json()["group"]["id"]
    assert client.delete(f"/api/library/{a}").status_code == 200
    d = client.get(f"/api/editions/{b}").json()
    assert d["group"]["id"] == gid and {e["id"] for e in d["editions"]} == {b, c}
    assert client.delete(f"/api/library/{b}").status_code == 200
    with Session(engine) as s:
        # Осталось одно издание — группа снята, книга жива.
        assert s.get(Work, c).edition_group_id is None
        assert s.get(EditionGroup, gid) is None


def test_group_progress_for_library_cards(client):
    a = add_work()
    b = add_work()
    solo = add_work()
    client.post(f"/api/editions/{a}/link", json={"other_id": b})
    put_progress(a, ratio=0.2, saved_at=100)
    put_progress(b, ratio=0.8, saved_at=200)
    put_progress(solo, ratio=0.5, saved_at=300)
    m = {int(k): v for k, v in client.get("/api/editions/progress").json().items()}
    assert m[a] == pytest.approx(0.8) and m[b] == pytest.approx(0.8)
    assert solo not in m


def test_migration_adds_columns_to_old_db(tmp_path, monkeypatch):
    """Старая БД без колонок изданий: init_db досоздаёт колонки, таблицу и индекс."""
    import sqlite3

    from sqlalchemy import create_engine, inspect
    from sqlmodel import SQLModel

    from backend.app.db import session as dbs

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE work (id INTEGER PRIMARY KEY, title VARCHAR DEFAULT '')")
    con.execute("INSERT INTO work (id, title) VALUES (1, 'old')")
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{db}")
    monkeypatch.setattr(dbs, "engine", eng)
    # create_all создаст недостающие таблицы, ALTER — колонки work.
    SQLModel.metadata.create_all(eng)
    added = dbs._migrate_add_columns()
    dbs._ensure_edition_index()
    assert {("work", "edition_group_id"), ("work", "language"), ("work", "edition_label")} <= added
    insp = inspect(eng)
    assert "edition_group" in insp.get_table_names()
    assert "ix_work_edition_group_id" in {i["name"] for i in insp.get_indexes("work")}
    with eng.connect() as c:
        from sqlalchemy import text
        assert c.execute(text("SELECT language, edition_group_id FROM work")).first() == ("", None)
