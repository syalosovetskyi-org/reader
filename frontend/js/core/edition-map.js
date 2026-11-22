// Сопоставление мест между изданиями одного произведения (serg/tasks#3240).
//
// Чистые функции без DOM — их проверяет tests/js/test_edition_map.mjs.
// То же правило глав, что и на сервере (backend/app/editions.py): номер в
// заголовке («Глава 5» ↔ «Chapter 5»), иначе порядковый номер со сдвигом на
// служебные страницы. Менять оба места вместе.

const NUM_RE = /(?:глава|гл\.|chapter|ch\.|часть|part|книга|book|розділ)\s*(\d+|[ivxlcdm]+)\b/i
const LEAD_NUM_RE = /^\s*(\d+)(?:[.:)\s]|$)/
const ROMAN = { i: 1, v: 5, x: 10, l: 50, c: 100, d: 500, m: 1000 }

function roman(s) {
  s = s.toLowerCase()
  if (!s || [...s].some((c) => !(c in ROMAN))) return null
  let t = 0
  for (let i = 0; i < s.length; i++) {
    const v = ROMAN[s[i]]
    t += i + 1 < s.length && ROMAN[s[i + 1]] > v ? -v : v
  }
  return t || null
}

export function chapterNumber(label) {
  const m = NUM_RE.exec(label || '')
  if (m) return /^\d+$/.test(m[1]) ? Number(m[1]) : roman(m[1])
  const l = LEAD_NUM_RE.exec(label || '')
  return l ? Number(l[1]) : null
}

// items: [{ label, index }] — пункты оглавления с номером секции. Глава —
// секция, с которой начинается пункт; подпункты внутри одной секции схлопываются.
export function buildChapters(items, sectionCount = 0) {
  const seen = new Set()
  const out = []
  for (const it of [...(items || [])].filter((x) => Number.isInteger(x?.index) && x.index >= 0)
    .sort((a, b) => a.index - b.index)) {
    if (seen.has(it.index) || (sectionCount && it.index >= sectionCount)) continue
    seen.add(it.index)
    out.push({ index: it.index, label: it.label || '', href: it.href || '' })
  }
  if (!out.length) for (let i = 0; i < sectionCount; i++) out.push({ index: i, label: `Раздел ${i + 1}`, href: '' })
  out.forEach((c, k) => { c.ordinal = k; c.number = chapterNumber(c.label) })
  return out
}

export function chapterAt(chapters, index) {
  let cur = null
  for (const c of chapters) { if (c.index <= index) cur = c; else break }
  return cur || chapters[0] || null
}

function span(chapters, ch, sectionCount) {
  const next = chapters[ch.ordinal + 1]
  return Math.max(1, (next ? next.index : sectionCount) - ch.index)
}

export function matchChapter(src, srcChapters, tgtChapters) {
  if (!tgtChapters.length || !src) return null
  if (src.number != null) {
    const hit = tgtChapters.find((c) => c.number === src.number)
    if (hit) return hit
  }
  let offset = 0
  const fs = srcChapters.find((c) => c.number != null)
  const ft = tgtChapters.find((c) => c.number != null)
  if (fs && ft) offset = ft.ordinal - fs.ordinal
  return tgtChapters[Math.max(0, Math.min(tgtChapters.length - 1, src.ordinal + offset))]
}

// Место в источнике (секция + доля в ней) → место в цели. src/tgt —
// { chapters, sections } (sections — число секций).
export function mapSection(src, tgt, index, fraction) {
  const sch = chapterAt(src.chapters, index)
  const tch = sch && matchChapter(sch, src.chapters, tgt.chapters)
  if (!tch) return null
  const sspan = span(src.chapters, sch, src.sections)
  const chFrac = Math.max(0, Math.min(1, (index - sch.index + (fraction || 0)) / sspan))
  const tspan = span(tgt.chapters, tch, tgt.sections)
  const pos = chFrac * tspan
  const step = Math.min(Math.floor(pos), tspan - 1)
  return { index: tch.index + step, fraction: Math.max(0, Math.min(1, pos - step)), chapter: tch, chapterFraction: chFrac }
}

// --- Поиск фрагмента текста: точно, потом нечётко --------------------------
// Слова нормализуем так, чтобы PDF↔EPUB одного перевода совпадали: регистр,
// ё/е, знаки препинания и кавычки не в счёт.
export function normWord(w) {
  return (w || '').toLowerCase().replace(/ё/g, 'е').replace(/[^\p{L}\p{N}]+/gu, '')
}
export function anchorWords(text) {
  return (text || '').split(/\s+/).map(normWord).filter(Boolean)
}

// hay, needle — массивы нормализованных слов. Возвращает { start, score, exact }
// или null. Нечёткое: окно длины needle с наибольшим пересечением мультимножеств;
// принимаем от minScore (доля слов needle, нашедшихся в окне).
export function findWords(hay, needle, minScore = 0.7) {
  const n = needle.length
  if (!n || hay.length < Math.min(n, 4) || n < 4) return null
  // Точное вхождение — по первому слову.
  for (let i = hay.indexOf(needle[0]); i >= 0 && i + n <= hay.length; i = hay.indexOf(needle[0], i + 1)) {
    let k = 1
    while (k < n && hay[i + k] === needle[k]) k++
    if (k === n) return { start: i, score: 1, exact: true }
  }
  const want = new Map()
  for (const w of needle) want.set(w, (want.get(w) || 0) + 1)
  const have = new Map()
  let hit = 0
  const add = (w, d) => {
    if (!want.has(w)) return
    const before = have.get(w) || 0
    const after = before + d
    have.set(w, after)
    hit += Math.min(after, want.get(w)) - Math.min(before, want.get(w))
  }
  let best = { start: -1, score: 0 }
  const len = Math.min(n, hay.length)
  for (let i = 0; i < hay.length; i++) {
    add(hay[i], 1)
    if (i >= len) add(hay[i - len], -1)
    if (i >= len - 1 && hit / n > best.score) best = { start: i - len + 1, score: hit / n }
  }
  return best.score >= minScore ? { ...best, exact: false } : null
}
