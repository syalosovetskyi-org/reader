"""Издания одного произведения (serg/tasks#3240): язык, карта глав, перенос места.

Издания — отдельные Work со своим прогрессом, группа их только связывает.
Здесь чистая логика без роутинга:

- язык книги: opf dc:language / fb2 <lang>, иначе по самому тексту;
- карта глав издания: секции файла (размер, линейность) + пункты оглавления
  из booktoc, сведённые к «одна глава = секция, с которой начинается пункт»;
- перенос места между изданиями: тот же текст — по text_anchor (ищет клиент,
  у него DOM), разные языки/переводы — по главе и доле внутри главы, PDF без
  текстового слоя — по доле книги (страницам).

Номера секций — те же, что у foliate-js (spine для EPUB, прямые потомки
первого <body> для FB2), как и в booktoc: клиент переходит по ним напрямую.
Зеркало сопоставления глав для разделённого экрана — frontend/js/core/edition-map.js;
правило одно, менять оба места вместе.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring, parse

from . import book_identity as bi
from . import booktoc
from .bookmeta import extract_meta, norm_lang

log = logging.getLogger("reader.editions")

NS_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
NS_OPF = "{http://www.idpf.org/2007/opf}"

# --------------------------------------------------------------------- язык

_CYR = re.compile(r"[а-яёіїєґ]", re.I)
_LAT = re.compile(r"[a-z]", re.I)
_UK = re.compile(r"[іїєґ]", re.I)


def language_by_text(text: str) -> str:
    """Грубое определение языка по буквам: кириллица → ru/uk, латиница → en.

    Не детектор языков, а различитель того, что лежит в библиотеке: русские
    переводы и английские оригиналы. Мало букв — «не знаю», а не угадывание.
    """
    sample = (text or "")[:20000]
    cyr = len(_CYR.findall(sample))
    lat = len(_LAT.findall(sample))
    if cyr + lat < 200:
        return ""
    if cyr > lat * 1.5:
        return "uk" if len(_UK.findall(sample)) > cyr * 0.02 else "ru"
    if lat > cyr * 1.5:
        return "en"
    return ""


def reading_file(work, fmt: str = "") -> tuple[Path, str] | None:
    """Файл, который открывает читалка, — тот же выбор, что у GET /file.

    PDF с готовой EPUB-версией читается как EPUB; fmt='pdf' — открыт сам PDF
    (у него оглавления на сервере нет — только доля). Ссылка на Calibre без
    локального файла даёт None: тянуть файл из Calibre ради карты глав нельзя.
    """
    want = (fmt or "").lower()
    file_format = (work.file_format or "").lower()
    converted = work.converted_path if work.converted_status == "ready" else ""
    if want != "pdf" and converted and Path(converted).exists():
        return Path(converted), "epub"
    if not work.file_path:
        return None
    p = Path(work.file_path)
    if not p.exists():
        log.warning("издание %s: файла нет на диске (%s)", work.id, p)
        return None
    return p, file_format or p.suffix.lstrip(".").lower()


def detect_language(work) -> str:
    """Язык издания: метаданные файла, иначе текст, иначе название."""
    rf = reading_file(work)
    if rf:
        path, fmt = rf
        if fmt in ("epub", "fb2"):
            lang = (extract_meta(str(path), fmt) or {}).get("language") or ""
            if lang:
                return lang
            by_text = language_by_text(bi.extract_text_sample(str(path), fmt, max_chars=20000))
            if by_text:
                return by_text
    # PDF без EPUB-версии и книги без файла: только название. Короткое название
    # language_by_text не различит (мало букв) — тогда честное «не знаю».
    title = (work.title or "") * 20
    return language_by_text(title)


def ensure_language(session, work) -> str:
    """Язык книги, при необходимости определить и запомнить (без commit)."""
    if work.language or work.language_manual:
        return work.language
    try:
        lang = detect_language(work)
    except (OSError, zipfile.BadZipFile, ParseError, DefusedXmlException, KeyError) as e:
        log.warning("язык издания %s не определён: %s", work.id, e)
        return ""
    if lang:
        work.language = norm_lang(lang)
        session.add(work)
    return work.language


# ---------------------------------------------------------------- карта глав

_NUM_RE = re.compile(
    r"(?:глава|гл\.|chapter|ch\.|часть|part|книга|book|розділ)\s*(\d+|[ivxlcdm]+)\b", re.I
)
_LEAD_NUM_RE = re.compile(r"^\s*(\d+)(?:[.:)\s]|$)")
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _roman(s: str) -> int | None:
    s = s.lower()
    if not s or any(c not in _ROMAN for c in s):
        return None
    total = 0
    for i, c in enumerate(s):
        v = _ROMAN[c]
        total += -v if i + 1 < len(s) and _ROMAN[s[i + 1]] > v else v
    return total or None


def chapter_number(label: str) -> int | None:
    """Номер главы из заголовка: «Глава 5», «Chapter V», «5. Встреча»."""
    m = _NUM_RE.search(label or "")
    if m:
        g = m.group(1)
        return int(g) if g.isdigit() else _roman(g)
    m = _LEAD_NUM_RE.match(label or "")
    return int(m.group(1)) if m else None


def _epub_sections(path: Path) -> list[dict]:
    """Секции EPUB в порядке spine: размер (как getSize у foliate) и линейность."""
    with zipfile.ZipFile(path) as zf:
        container = fromstring(zf.read("META-INF/container.xml"))
        rootfile = container.find(f".//{NS_CONTAINER}rootfile")
        opf_path = (rootfile.get("full-path") if rootfile is not None else "") or ""
        if not opf_path:
            return []
        opf = fromstring(zf.read(opf_path))
        manifest = {
            (it.get("id") or ""): (it.get("href") or "")
            for it in opf.iter(f"{NS_OPF}item")
        }
        sizes = {i.filename: i.file_size for i in zf.infolist()}
        out = []
        spine = opf.find(f"{NS_OPF}spine")
        for ref in (spine.iter(f"{NS_OPF}itemref") if spine is not None else []):
            href = booktoc._resolve(opf_path, manifest.get(ref.get("idref") or "", ""))
            out.append({"href": href, "size": int(sizes.get(href, 0)),
                        "linear": (ref.get("linear") or "yes") != "no"})
        return out


def _fb2_sections(path: Path) -> list[dict]:
    """Секции FB2 = прямые потомки первого <body>; размер — длина их текста."""
    root = parse(path).getroot()
    bodies = [el for el in root if booktoc._local(el.tag) == "body"]
    if not bodies:
        return []
    return [{"href": str(i), "size": len("".join(ch.itertext())), "linear": True}
            for i, ch in enumerate(bodies[0])]


def chapter_map(work, fmt: str = "") -> dict:
    """Карта глав издания: {format, sections:[{size, linear}], chapters:[…]}.

    Глава — секция, с которой начинается пункт оглавления (подглавы внутри
    одной секции схлопываются в первый пункт). Нет оглавления — главой
    считается каждая линейная секция. PDF без EPUB-версии — пустая карта:
    там переносим только долю книги.
    """
    rf = reading_file(work, fmt)
    if not rf:
        return {"format": (work.file_format or "").lower(), "sections": [], "chapters": []}
    path, rfmt = rf
    try:
        if rfmt == "epub" or zipfile.is_zipfile(path):
            sections = _epub_sections(path)
        elif rfmt == "fb2":
            sections = _fb2_sections(path)
        else:
            return {"format": rfmt, "sections": [], "chapters": []}
    except (OSError, zipfile.BadZipFile, ParseError, DefusedXmlException, KeyError) as e:
        # Пустая карта ≠ «глав нет»: перенос деградирует до доли книги, и это
        # видно в ответе (method=ratio), а причина — здесь.
        log.warning("карта глав издания %s (%s) не построена: %s", work.id, path, e)
        return {"format": rfmt, "sections": [], "chapters": []}

    items = booktoc.toc_for(work.id, path, rfmt)
    seen: set[int] = set()
    chapters: list[dict] = []
    for it in sorted((i for i in items if isinstance(i.get("index"), int) and i["index"] >= 0),
                     key=lambda i: i["index"]):
        if it["index"] in seen or it["index"] >= len(sections):
            continue
        seen.add(it["index"])
        chapters.append({"index": it["index"], "label": it.get("label") or "",
                         "href": it.get("href") or ""})
    if not chapters:
        chapters = [{"index": i, "label": f"Раздел {i + 1}", "href": s["href"]}
                    for i, s in enumerate(sections) if s["linear"]]
    for k, ch in enumerate(chapters):
        ch["ordinal"] = k
        ch["number"] = chapter_number(ch["label"])
    return {"format": rfmt, "sections": [{"size": s["size"], "linear": s["linear"]} for s in sections],
            "chapters": chapters}


# ------------------------------------------------------- позиция и перенос

_CFI_SPINE = re.compile(r"^epubcfi\(/6/(\d+)")


def section_of(prog, sections: list[dict]) -> tuple[int, float] | None:
    """(номер секции, доля внутри секции) сохранённой позиции.

    Секцию берём из CFI (/6/N → N/2-1) — она точна. Долю внутри секции — из
    общей доли и размеров секций, как её считает foliate (SectionProgress):
    приблизительно, но для «того же места в другом переводе» этого достаточно.
    """
    sizes = [s["size"] if s["linear"] and s["size"] > 0 else 0 for s in sections]
    total = sum(sizes)
    if not sections or not total:
        return None
    ratio = max(0.0, min(1.0, float(getattr(prog, "ratio", 0) or 0)))
    idx = None
    m = _CFI_SPINE.match(getattr(prog, "locator", "") or "")
    if m and "!" in (prog.locator or ""):
        cand = int(m.group(1)) // 2 - 1
        if 0 <= cand < len(sections):
            idx = cand
    acc = 0.0
    if idx is None:
        for i, s in enumerate(sizes):
            if s and (acc + s) / total >= ratio:
                idx = i
                break
            acc += s
        if idx is None:
            idx = max(i for i, s in enumerate(sizes) if s)
            acc = total - sizes[idx]
    else:
        acc = sum(sizes[:idx])
    size = sizes[idx]
    frac = (ratio * total - acc) / size if size else 0.0
    return idx, max(0.0, min(1.0, frac))


def chapter_at(chapters: list[dict], index: int) -> dict | None:
    cur = None
    for ch in chapters:
        if ch["index"] <= index:
            cur = ch
        else:
            break
    return cur or (chapters[0] if chapters else None)


def _span(chapters: list[dict], ch: dict, n_sections: int) -> int:
    k = ch["ordinal"]
    end = chapters[k + 1]["index"] if k + 1 < len(chapters) else n_sections
    return max(1, end - ch["index"])


def match_chapter(src: dict, src_chapters: list[dict], tgt_chapters: list[dict]) -> dict | None:
    """Глава цели для главы источника.

    1) совпадающий номер в заголовке («Глава 5» ↔ «Chapter 5»);
    2) иначе — порядковый номер, сдвинутый на разницу служебных страниц:
       если в обоих изданиях есть нумерованные главы, их первые совмещаются.
    """
    if not tgt_chapters:
        return None
    if src.get("number") is not None:
        for ch in tgt_chapters:
            if ch.get("number") == src["number"]:
                return ch
    offset = 0
    first_s = next((c["ordinal"] for c in src_chapters if c.get("number") is not None), None)
    first_t = next((c["ordinal"] for c in tgt_chapters if c.get("number") is not None), None)
    if first_s is not None and first_t is not None:
        offset = first_t - first_s
    k = max(0, min(len(tgt_chapters) - 1, src["ordinal"] + offset))
    return tgt_chapters[k]


def norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def same_text(a, b) -> bool:
    """Один ли текст у двух изданий (PDF↔EPUB одного перевода).

    Тот же язык и метки не противоречат друг другу. Неизвестный язык у обоих
    считаем «тем же» — клиент всё равно ищет фрагмент сперва точно, и при
    промахе уходит на главу, так что ошибка здесь стоит одного поиска.
    """
    if (a.language or "") != (b.language or ""):
        return False
    la, lb = norm_label(a.edition_label), norm_label(b.edition_label)
    return not la or not lb or la == lb



# Серж 22.11.2026: «не делать конвертацию в EPUB книг, для которых есть
# официальный EPUB-файл уже». Официальный = издание той же группы с тем же
# текстом (same_text: язык и метка перевода) в перетекающем формате.
FLOWING_FORMATS = {"epub", "fb2"}


def official_epub_for(session, work):
    """Издание-EPUB того же текста для PDF/DJVU-издания или None."""
    from sqlmodel import select

    from .db.models import Work

    if not getattr(work, "edition_group_id", None):
        return None
    if (work.file_format or "").lower() in FLOWING_FORMATS:
        return None
    siblings = session.exec(
        select(Work).where(Work.edition_group_id == work.edition_group_id, Work.id != work.id)
    ).all()
    for w in siblings:
        if (w.file_format or "").lower() in FLOWING_FORMATS and w.file_path and same_text(work, w):
            return w
    return None

def map_position(src_work, src_prog, src_fmt: str, tgt_work, tgt_fmt: str) -> dict:
    """Куда в издании-цели ставить читателя, стоявшего в src_prog.

    method:
      anchor  — тот же текст: искать text_anchor (точно, потом нечётко) в
                главе target; не нашёлся — начало этой главы;
      chapter — разные языки/переводы: глава target + доля внутри главы;
      ratio   — нет карты глав хотя бы с одной стороны (PDF без текста):
                доля книги, то есть страница.
    """
    base = {
        "ratio": float(src_prog.ratio or 0.0),
        "text_anchor": src_prog.text_anchor or "",
        "source_chapter": src_prog.chapter or "",
        "same_text": same_text(src_work, tgt_work),
    }
    src_is_pdf = (src_fmt or src_prog.file_format or "").lower() == "pdf" or (
        (src_work.file_format or "").lower() == "pdf" and not src_prog.file_format
        and "!" not in (src_prog.locator or ""))
    tgt_is_pdf = (tgt_fmt or "").lower() == "pdf"
    if src_is_pdf or tgt_is_pdf:
        return {**base, "method": "ratio", "target": None}
    smap = chapter_map(src_work, "epub")
    tmap = chapter_map(tgt_work, tgt_fmt)
    loc = section_of(src_prog, smap["sections"])
    if not loc or not smap["chapters"] or not tmap["chapters"]:
        return {**base, "method": "ratio", "target": None}
    sidx, sfrac = loc
    sch = chapter_at(smap["chapters"], sidx)
    span = _span(smap["chapters"], sch, len(smap["sections"]))
    ch_frac = max(0.0, min(1.0, (sidx - sch["index"] + sfrac) / span))
    tch = match_chapter(sch, smap["chapters"], tmap["chapters"])
    tspan = _span(tmap["chapters"], tch, len(tmap["sections"]))
    pos = ch_frac * tspan
    step = min(int(pos), tspan - 1)
    tidx = tch["index"] + step
    tfrac = max(0.0, min(1.0, pos - step))
    return {
        **base,
        "method": "anchor" if base["same_text"] and base["text_anchor"] else "chapter",
        "chapter_fraction": ch_frac,
        "source": {"index": sidx, "label": sch["label"], "number": sch["number"], "ordinal": sch["ordinal"]},
        "target": {
            "chapter_index": tch["index"], "label": tch["label"], "href": tch["href"],
            "number": tch["number"], "ordinal": tch["ordinal"], "span": tspan,
            "index": tidx, "fraction": tfrac,
        },
    }


# ----------------------------------------------------------- подсказки

def _title_score(a: str, b: str) -> float:
    plain = bi.title_sim(a, b)
    cross = bi._ratio(bi._lat2cyr(bi._title_key(a)[0]), bi._lat2cyr(bi._title_key(b)[0]))
    return max(plain, cross if bi._title_key(a)[0] and bi._title_key(b)[0] else 0.0)


# Пороги подсказки «похоже на то же произведение». Подсказка — не связь:
# её подтверждает человек, поэтому порог мягче дедупа, но книга-тёзка
# другого автора проходит только при почти полном совпадении названия.
TITLE_STRONG = 0.92
TITLE_WITH_AUTHOR = 0.6


def suggestion_score(a, b) -> float:
    """0 — не подсказываем; иначе чем больше, тем вероятнее то же произведение."""
    t = _title_score(a.title or "", b.title or "")
    author = bi.author_relation(a.author or "", b.author or "")
    if author == bi.MATCH and t >= TITLE_WITH_AUTHOR:
        return 0.5 + t / 2
    if author != bi.CONFLICT and t >= TITLE_STRONG:
        return t / 2
    return 0.0
