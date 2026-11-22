// Поиск по книге — по мере ввода.
//
// Ищем сами по кэшу нормализованного текста секций (core/text-search.js):
// foliate view.search() на каждый запрос заново разбирает все секции и
// сравнивает Intl.Collator-ом посимвольно, и при наборе по букве это секунды
// на каждую букву. Здесь первая буква прогревает кэш, дальше каждый запрос —
// indexOf по готовым строкам. view.search() остаётся только для подсветки
// совпадений в той секции, что сейчас на экране (одна секция — это быстро).
import { $, escapeHtml } from './core/dom.js'
import { view } from './core/state.js'
import { logErr } from './core/log.js'
import { closePanels } from './navigation.js'
import { navigate } from './jumps.js'
import { textWalker } from '../vendor/foliate-js/text-walker.js'
import { normalizeStrs, normalizeQuery, findInSection } from './core/text-search.js'

const DEBOUNCE_MS = 220
const MIN_LIVE_LEN = 2   // одну букву ищем только по Enter: тысячи совпадений бесполезны
const MAX_SHOWN = 300    // больше строк в списке не рисуем, считаем дальше

// ---------- кэш текста секций, отдельно для каждой открытой книги ----------
const caches = new WeakMap() // book → { sections: Map<i, Promise<entry|null>>, labels }

function bookCache(book) {
  let c = caches.get(book)
  if (!c) { c = { sections: new Map(), labels: null }; caches.set(book, c) }
  return c
}

function sectionText(book, i) {
  const c = bookCache(book)
  let p = c.sections.get(i)
  if (!p) {
    p = (async () => {
      const s = book.sections[i]
      if (!s?.createDocument) return null
      const doc = await s.createDocument()
      const { value } = textWalker(doc, function* (strs, makeRange) { yield { strs, makeRange } }).next()
      if (!value) return null
      const raw = value.strs.join('')
      return { sec: normalizeStrs(value.strs), raw, makeRange: value.makeRange }
    })().catch((e) => {
      // Битая секция не должна ронять весь поиск: пишем в лог и считаем её пустой.
      logErr(`поиск: секция ${i} не открылась`, e)
      return null
    })
    c.sections.set(i, p)
  }
  return p
}

// Подписи глав для строк результатов: индекс секции → ближайший пункт оглавления.
async function sectionLabels(book) {
  const c = bookCache(book)
  if (c.labels) return c.labels
  const byIndex = new Map()
  const flat = []
  const walk = (items) => { for (const it of items || []) { flat.push(it); walk(it.subitems) } }
  walk(book.toc)
  for (const it of flat) {
    if (!it.href) continue
    try {
      const r = await book.resolveHref?.(it.href)
      if (r && Number.isInteger(r.index) && !byIndex.has(r.index)) byIndex.set(r.index, it.label || '')
    } catch (e) {
      // Пункт оглавления без цели: подпись возьмётся у предыдущей главы.
      logErr('поиск: пункт оглавления не разрешился', e)
    }
  }
  const labels = []
  let last = ''
  for (let i = 0; i < book.sections.length; i++) {
    if (byIndex.has(i)) last = byIndex.get(i)
    labels.push(String(last).trim())
  }
  c.labels = labels
  return labels
}

const tick = () => new Promise((r) => setTimeout(r, 0))

// Прогрев кэша, пока человек только открыл панель и печатает.
let warming = null
function prewarm() {
  const book = view?.book
  if (!book || warming === book) return
  warming = book
  ;(async () => {
    sectionLabels(book).catch((e) => logErr('поиск: оглавление', e))
    for (let i = 0; i < book.sections.length; i++) {
      if (view?.book !== book) return // книгу закрыли — бросаем
      await sectionText(book, i)
      await tick()
    }
  })().catch((e) => logErr('поиск: прогрев', e))
}

// ---------- сам поиск ----------
let searchSeq = 0
let active = null // { terms, book } — что подсвечивать в видимой секции

async function runSearch(q) {
  const seq = ++searchSeq
  const results = $('#search-results')
  const meta = $('#search-meta')
  results.innerHTML = ''
  const book = view?.book
  if (!book) return
  const terms = q.split('|').map((t) => t.trim()).filter(Boolean)
  const needles = terms.map(normalizeQuery).filter(Boolean)
  if (!needles.length) {
    active = null
    view.clearSearch?.()
    meta.textContent = ''
    return
  }
  active = { terms, book }
  const isMulti = needles.length > 1
  const total = book.sections.length
  const warn = view.book?.rangeMode ? 'Поиск загружает всю книгу… ' : ''
  const labels = await sectionLabels(book).catch(() => [])
  if (seq !== searchSeq) return
  let count = 0
  let shown = 0
  let lastPaint = 0
  for (let i = 0; i < total; i++) {
    const entry = await sectionText(book, i)
    if (seq !== searchSeq) return
    if (entry) {
      for (let t = 0; t < needles.length; t++) {
        const hits = findInSection(entry.sec, entry.raw, needles[t], MAX_SHOWN - shown + 1)
        const exact = hits.length > MAX_SHOWN - shown
          ? findInSection(entry.sec, entry.raw, needles[t]).length // считаем все, рисуем часть
          : hits.length
        count += exact
        for (const h of hits) {
          if (shown >= MAX_SHOWN) break
          const lbl = (labels[i] || '') + (isMulti ? ` [${terms[t]}]` : '')
          results.append(searchResult(lbl, book, i, entry, h))
          shown++
        }
      }
    }
    // Обновлять подпись и отдавать поток браузеру не на каждой секции, а раз
    // в ~50 мс: ввод не тормозит, а секции из кэша идут подряд без пауз.
    const now = performance.now()
    if (now - lastPaint > 50) {
      lastPaint = now
      meta.textContent = warn + `Поиск… ${Math.round(((i + 1) / total) * 100)}% · найдено ${count}`
      await tick()
      if (seq !== searchSeq) return
    }
  }
  meta.textContent = !count ? 'Ничего не найдено'
    : count > shown ? `Найдено: ${count} (показаны первые ${shown} — уточните запрос)`
      : `Найдено: ${count}`
  highlightVisible()
}

function searchResult(label, book, index, entry, hit) {
  const ex = hit.excerpt
  const a = document.createElement('a')
  a.className = 'search-result'; a.href = '#'
  a.innerHTML =
    (label ? `<span class="sr-label">${escapeHtml(label)}</span>` : '') +
    `<span class="sr-ex">${escapeHtml(ex.pre)}<mark>${escapeHtml(ex.match)}</mark>${escapeHtml(ex.post)}</span>`
  a.addEventListener('click', (ev) => {
    ev.preventDefault()
    if (view?.book !== book) return
    let cfi
    try {
      const { startIndex, startOffset, endIndex, endOffset } = hit.range
      cfi = view.getCFI(index, entry.makeRange(startIndex, startOffset, endIndex, endOffset))
    } catch (e) {
      logErr('поиск: не построил ссылку на найденное', e)
      return
    }
    // Переход к найденному — прыжок: место, откуда искали, остаётся в
    // истории переходов (serg/tasks#1078).
    navigate('search', () => view.goTo(cfi)).catch((e) => logErr('переход к найденному не удался', e))
    closePanels()
  })
  return a
}

// Подсветка совпадений в секции на экране — силами foliate (он же снимет её
// при clearSearch и перерисует при перелистывании внутри секции).
let hlSeq = 0
let hlIndex = -1
async function highlightVisible() {
  const v = view
  if (!v || !active || active.book !== v.book) return
  const index = v.lastLocation?.section?.current
  if (!Number.isInteger(index)) return
  hlIndex = index
  const my = ++hlSeq
  try {
    for await (const r of v.search({ query: active.terms[0], index })) {
      if (my !== hlSeq || r === 'done') break
    }
  } catch (e) {
    logErr('поиск: подсветка не удалась', e)
  }
}

const watched = new WeakSet()
function watchRelocate() {
  const v = view
  if (!v || watched.has(v)) return
  watched.add(v)
  v.addEventListener('relocate', () => {
    const idx = v.lastLocation?.section?.current
    if (active && idx !== hlIndex) highlightVisible()
  })
}

// ---------- ввод ----------
let timer = null
function schedule(now) {
  clearTimeout(timer)
  const q = $('#search-input').value.trim()
  if (!view) return
  watchRelocate()
  prewarm()
  if (!now && q && normalizeQuery(q).replace(/\|/g, '').length < MIN_LIVE_LEN) {
    searchSeq++ // старый поиск по более длинной строке больше не нужен
    $('#search-meta').textContent = 'Наберите ещё букву — или Enter'
    return
  }
  const run = () => runSearch(q).catch((e) => {
    logErr('поиск не удался', e)
    $('#search-meta').textContent = 'Ошибка поиска: ' + (e?.message || '')
  })
  if (now) run()
  else timer = setTimeout(run, DEBOUNCE_MS)
}

$('#search-form').addEventListener('submit', (e) => { e.preventDefault(); schedule(true) })
$('#search-input').addEventListener('input', () => schedule(false))
$('#search-btn')?.addEventListener('click', () => { if (view) { watchRelocate(); prewarm() } })

// Открыли другую книгу — прошлый запрос к ней не относится.
document.addEventListener('reader-opening', () => {
  searchSeq++; active = null; hlIndex = -1; clearTimeout(timer)
})
