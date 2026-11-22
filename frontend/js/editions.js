// Издания одного произведения в читалке и на странице книги (serg/tasks#3240).
//
// Издания — отдельные книги со своим прогрессом; группа их только связывает.
//  * тулбар: переключатель издания, быстрый RU↔EN, «Два издания рядом»;
//  * при открытии: если в группе есть позиция свежее — встать на то же место
//    (тот же текст — по фрагменту, разные языки — по главе и доле) и показать
//    тост с «Вернуть»; не нашли место — начало главы и тост до закрытия;
//  * разделённый экран: две foliate-view рядом (на телефоне — сверху и снизу),
//    ведущая — та, которую листали последней, вторая идёт следом по главам;
//  * страница книги: блок «Издания» — список, связь, отвязка, синхронизация.
//
// Переход на другое издание — ОДНА функция openEdition(): её обернёт
// navigate('edition', …) из serg/tasks#3239.
import { $, escapeHtml, toast } from './core/dom.js'
import { api } from './core/api.js'
import { logErr } from './core/log.js'
import { icon } from './core/icons.js'
import { view, currentWork, libWorks } from './core/state.js'
import { openReader, settlePosition, currentFormat, currentRevision, applyViewStyles } from './reader-core.js'
import { gotoPosition, captureAnchor } from './core/position.js'
import { serverSavedAt, commitPosition } from './core/progress-sync.js'
import { buildChapters, mapSection, anchorWords, normWord, findWords } from './core/edition-map.js'

// ------------------------------------------------------------- подписи

const lang = (e) => (e?.language || '?').toUpperCase()
function edName(e) {
  const l = (e?.edition_label || '').trim()
  if (l) return l
  return (e?.file_format || '').toUpperCase() || 'издание'
}
// «Оригинал» / «Перевод: Катаев» — как в макете; метка «перевод …» уже говорит сама.
function edTitle(e) {
  const l = (e?.edition_label || '').trim()
  if (!l) return edName(e)
  if (/^(оригинал|original)$/i.test(l)) return 'Оригинал'
  return /^перевод/i.test(l) ? l[0].toUpperCase() + l.slice(1) : l
}
const edShort = (e) => `${lang(e)} · ${edName(e)}`
const pctOf = (e) => Math.round(Math.min(1, e?.ratio || 0) * 100)
const isOriginal = (e) => /^(оригинал|original)$/i.test((e?.edition_label || '').trim())

function chapterText(t) {
  if (!t) return ''
  return t.number != null ? `Глава ${t.number}` : (t.label || '')
}

// Момент своей позиции; пустая (место ещё не сохраняли) — 0, как на сервере.
function ownStamp(prog) {
  if (!prog || !(prog.locator || prog.text_anchor || prog.ratio)) return 0
  return serverSavedAt(prog)
}

// ---------------------------------------------------- состояние читалки

let group = null      // ответ GET /api/editions/{id} для открытой книги
let skipSyncOnce = 0  // id издания, которое открываем на его СОБСТВЕННОМ месте

async function loadGroup(workId) {
  return api.get(`/api/editions/${workId}`)
}

function others(payload) {
  return (payload?.editions || []).filter((e) => !e.current)
}
function currentEd(payload) {
  return (payload?.editions || []).find((e) => e.current) || null
}
function otherLanguage(payload) {
  const cur = currentEd(payload)
  const list = others(payload).filter((e) => e.language && e.language !== cur?.language)
  // Перевод → в первую очередь оригинал: кнопка читается как «Оригинал / EN».
  return list.find(isOriginal) || list[0] || null
}

// «Перевести на русский» не нужен, если книга уже по-русски или рядом есть
// русское издание: для переводной книги вместо него — переключатель издания.
function paintTranslate(payload) {
  const cur = currentEd(payload)
  const hide = cur?.language === 'ru'
    || others(payload).some((e) => e.language === 'ru')
  for (const id of ['#translate-btn', '#tr-wide']) { const b = $(id); if (b) b.hidden = hide }
}

function paintToolbar(payload) {
  const sw = $('#ed-switch'), quick = $('#ed-quick'), split = $('#split-btn')
  const grouped = (payload?.editions || []).length > 1
  sw.hidden = quick.hidden = split.hidden = !grouped
  document.body.classList.toggle('has-editions', grouped)
  paintTranslate(payload)
  if (!grouped) { closeMenu(); return }
  const cur = currentEd(payload)
  sw.innerHTML = `<span class="lang">${escapeHtml(lang(cur))}</span><span class="ed-sw-name"> · ${escapeHtml(edName(cur))}</span>${icon('chevron-down', 16)}`
  sw.setAttribute('aria-label', `Издание: ${edShort(cur)}. Выбрать другое`)
  sw.title = 'Издание'
  const other = otherLanguage(payload)
  quick.hidden = !other
  if (other) {
    quick.innerHTML = `${icon('exchange', 18)}<span class="q-txt">${escapeHtml(lang(cur))}→${escapeHtml(lang(other))}</span>`
    quick.setAttribute('aria-label', `Переключить на ${lang(other)}`)
    quick.title = isOriginal(other) ? `Оригинал / ${lang(other)}` : `Издание ${edShort(other)}`
    quick.dataset.target = String(other.id)
  }
}

// ------------------------------------------------------------ меню издания

function closeMenu() {
  const m = $('#ed-menu')
  if (m && !m.hidden) m.hidden = true
  $('#ed-switch')?.setAttribute('aria-expanded', 'false')
}

function openMenu() {
  const m = $('#ed-menu'), sw = $('#ed-switch')
  if (!group) return
  const rows = (group.editions || []).map((e) => `
    <button class="mi" role="menuitem" type="button" data-ed="${e.id}" aria-current="${e.current}">
      <span class="ed-badge lang">${escapeHtml(lang(e))}</span>
      <span><b>${escapeHtml(edTitle(e))}</b><small>${escapeHtml((e.file_format || '').toUpperCase())} · ${pctOf(e)}%${e.chapter ? ' · ' + escapeHtml(e.chapter) : ''}</small></span>
      ${e.current ? icon('check') : '<span></span>'}
    </button>`).join('')
  m.innerHTML = rows + `<div class="sep"></div>
    <button class="mi" role="menuitem" type="button" data-act="split"><span>${icon('columns')}</span><span><b>Два издания рядом</b></span><span></span></button>
    <button class="mi" role="menuitem" type="button" data-act="sync"><span>${icon('refresh')}</span><span><b>Синхронизация</b><small>${group.group?.sync ? 'включена' : 'выключена'}</small></span><span></span></button>`
  const r = sw.getBoundingClientRect()
  m.style.top = `${Math.round(r.bottom + 4)}px`
  m.style.left = `${Math.max(6, Math.round(r.left))}px`
  m.hidden = false
  sw.setAttribute('aria-expanded', 'true')
  m.querySelector('.mi')?.focus()
}

async function toggleSync() {
  if (!group?.group || !currentWork) return
  try {
    group = await api.put(`/api/editions/${currentWork.id}/sync`, { sync: !group.group.sync })
    toast(group.group.sync ? 'Синхронизация позиции включена' : 'Синхронизация позиции выключена', 'info')
  } catch (e) {
    logErr('editions: sync toggle', e)
    toast('Не удалось переключить синхронизацию: ' + e.message, 'err')
  }
}

// ------------------------------------------------ переход на другое издание

// Единственная точка перехода на другое издание. Сначала дожидаемся, пока
// место текущего издания легло на сервер: по нему цель и встанет «на то же
// место» (позиция группы = последняя по времени).
export async function openEdition(target, opts = {}) {
  const id = Number(typeof target === 'object' ? target?.id : target)
  if (!id) return
  closeMenu()
  hideToast(); hideChip()
  await settlePosition()
  let work = libWorks.find((w) => w.id === id)
  try { work = { ...(work || {}), ...await api.get(`/api/library/${id}`) } } catch (e) {
    logErr('editions: деталь издания', e)
    if (!work) { toast('Издание не открылось: ' + e.message, 'err'); return }
  }
  if (opts.ownPlace) skipSyncOnce = id
  await openReader(work)
}

// ---------------------------------------------- поиск места в издании-цели

// Слова секции с их узлами — чтобы найденный фрагмент превратить в Range.
function wordMap(doc) {
  const root = doc?.body || doc?.documentElement
  if (!root) return { words: [], at: [] }
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const words = [], at = []
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const re = /\S+/g
    let m
    while ((m = re.exec(n.nodeValue))) {
      const w = normWord(m[0])
      if (!w) continue
      words.push(w)
      at.push({ node: n, start: m.index, end: m.index + m[0].length })
    }
  }
  return { words, at }
}

// Фрагмент text_anchor в секциях главы (и соседних): сперва точно во всех,
// потом нечётко. Возвращает CFI или null.
async function findAnchor(v, anchor, indices) {
  const needle = anchorWords(anchor)
  if (needle.length < 4) return null
  const sections = v.book?.sections || []
  const docs = []
  for (const i of indices) {
    if (i < 0 || i >= sections.length || !sections[i]?.createDocument) continue
    try { docs.push([i, await sections[i].createDocument()]) } catch (e) { logErr(`editions: секция ${i} не открылась`, e) }
  }
  const maps = docs.map(([i, doc]) => [i, doc, wordMap(doc)])
  for (const exactOnly of [true, false]) {
    for (const [i, doc, map] of maps) {
      const hit = findWords(map.words, needle, exactOnly ? 2 : 0.7)
      if (!hit || (exactOnly && !hit.exact)) continue
      const a = map.at[hit.start], b = map.at[Math.min(map.at.length - 1, hit.start + needle.length - 1)]
      const range = doc.createRange()
      range.setStart(a.node, a.start)
      range.setEnd(b.node, b.end)
      try { return v.getCFI(i, range) } catch (e) { logErr('editions: CFI найденного фрагмента', e) }
    }
  }
  return null
}

// Поставить открытую view на место из sync-target. → { outcome, text }
async function place(v, t) {
  if (t.method === 'ratio' || !t.target) {
    await v.goToFraction(Math.max(0, Math.min(1, t.ratio || 0)))
    return { outcome: 'ratio', text: `${Math.round((t.ratio || 0) * 100)}% книги, по страницам` }
  }
  const tg = t.target
  // Номер секции сервер считает по spine; сверяем по href главы — он точен.
  let chapterIndex = tg.chapter_index
  if (tg.href) {
    try {
      const r = v.resolveNavigation(tg.href)
      if (Number.isInteger(r?.index)) chapterIndex = r.index
    } catch (e) { logErr('editions: href главы', e) }
  }
  const shift = chapterIndex - tg.chapter_index
  const ch = chapterText(tg)
  if (t.method === 'anchor') {
    const span = Math.max(1, tg.span || 1)
    const idx = [...Array(span).keys()].map((k) => chapterIndex + k)
    idx.push(chapterIndex - 1, chapterIndex + span)
    const cfi = await findAnchor(v, t.text_anchor, idx)
    if (cfi) { await v.goTo(cfi); return { outcome: 'exact', text: `${ch}, тот же абзац` } }
    await v.renderer.goTo({ index: chapterIndex, anchor: () => 0 })
    return { outcome: 'start', text: ch }
  }
  await v.renderer.goTo({ index: tg.index + shift, anchor: () => tg.fraction || 0 })
  return { outcome: 'chapter', text: ch }
}

// ---------------------------------------------------------- тост и плашка

let toastTimer = null, chipTimer = null
function hideToast() { clearTimeout(toastTimer); const t = $('#ed-toast'); if (t) { t.hidden = true; t.innerHTML = '' } }
function hideChip() { clearTimeout(chipTimer); const c = $('#ed-chip'); if (c) { c.hidden = true; c.innerHTML = '' } }

function showToast({ warn = false, title, text, action, onAction, sticky = false }) {
  const t = $('#ed-toast')
  hideToast()
  t.className = 'ed-toast' + (warn ? ' warn' : '')
  t.innerHTML = `<span class="t"><b>${escapeHtml(title)}</b>${escapeHtml(text || '')}</span>`
    + (action ? `<button class="lnk" type="button" data-act="action">${escapeHtml(action)}</button>` : '')
    + `<button class="icon-btn" type="button" data-act="close" aria-label="Закрыть">${icon('x')}</button>`
  t.hidden = false
  t.querySelector('[data-act="close"]').addEventListener('click', hideToast)
  t.querySelector('[data-act="action"]')?.addEventListener('click', () => { hideToast(); onAction?.() })
  if (!sticky) toastTimer = setTimeout(hideToast, 4000)
}

function showChip({ text, onWhere }) {
  const c = $('#ed-chip')
  hideChip()
  c.innerHTML = `${icon('refresh', 18)}<span class="sp">${text}</span>`
    + `<button type="button" data-act="where">Откуда</button>`
    + `<button type="button" data-act="x" aria-label="Скрыть">${icon('x', 16)}</button>`
  c.hidden = false
  c.querySelector('[data-act="where"]').addEventListener('click', () => { hideChip(); onWhere?.() })
  c.querySelector('[data-act="x"]').addEventListener('click', hideChip)
  chipTimer = setTimeout(hideChip, 6000)
}

// --------------------------------------------------- открытие издания

async function onReaderOpened(ev) {
  const { work, prog, fmt, view: v, current } = ev.detail || {}
  hideToast(); hideChip(); closeMenu()
  if (!work) return
  let payload = null
  try { payload = await loadGroup(work.id) } catch (e) {
    // Без списка изданий читалка работает как раньше: тулбар без изданий.
    logErr('editions: список изданий', e)
  }
  if (!current()) return
  group = payload
  paintToolbar(payload)
  const skip = skipSyncOnce === work.id
  skipSyncOnce = 0
  if (skip || !payload?.group?.sync || (payload.editions || []).length < 2) return
  let t
  try {
    t = await api.get(`/api/editions/${work.id}/sync-target?fmt=${encodeURIComponent(fmt || '')}&own_saved_at=${ownStamp(prog)}`)
  } catch (e) {
    logErr('editions: позиция группы', e)
    toast('Не удалось узнать место в других изданиях — открыто своё', 'err')
    return
  }
  if (!current() || !t?.apply) return
  let res
  try { res = await place(v, t) } catch (e) {
    logErr('editions: перенос места', e)
    toast('Не удалось перенести место из другого издания — открыто своё', 'err')
    return
  }
  if (!current()) return
  // «Вернуть» — на собственное место издания; места ещё не было — в начало текста.
  const back = () => (ownStamp(prog) ? gotoPosition(v, prog, currentRevision(), fmt) : v.goToTextStart().then(() => true))
    .then((ok) => { if (!ok) toast('Не удалось вернуться к прежнему месту', 'err') })
    .catch((e) => { logErr('editions: «Вернуть»', e); toast('Не удалось вернуться к прежнему месту', 'err') })
  if (res.outcome === 'start') {
    showToast({ warn: true, sticky: true, title: 'Точное место не найдено.',
      text: ` Открыто начало: ${res.text}. В этом издании абзац передан иначе.`,
      action: 'Выбрать место',
      onAction: () => import('./navigation.js').then((m) => m.openPanel('#toc-panel'))
        .catch((e) => logErr('editions: оглавление', e)) })
    return
  }
  const srcFmt = (t.source_format || '').toUpperCase()
  if (t.same_text && srcFmt && srcFmt !== String(fmt || '').toUpperCase()) {
    showChip({ text: `<b>Позиция из ${escapeHtml(srcFmt)}</b>`, onWhere: () => openSplit([t.source_id, work.id], 1) })
    return
  }
  showToast({ title: 'Открыто на том же месте:', text: ` ${res.text}`, action: 'Вернуть', onAction: back })
}

// -------------------------------------------------------- разделённый экран

const S = { pair: [], views: [null, null], maps: [null, null], last: [null, null], fmts: ['', ''], ready: [false, false],
  lead: 0, sync: true, orient: 'auto', syncing: 0, menu: null, gen: 0 }

const splitRows = () => S.orient === 'rows' || (S.orient === 'auto' && innerWidth < 720)

function flatToc(items, out = []) {
  for (const it of items || []) { out.push(it); flatToc(it.subitems, out) }
  return out
}

function chaptersOf(v) {
  const items = flatToc(v.book?.toc).map((it) => {
    let index = -1
    try { index = v.resolveNavigation(it.href)?.index ?? -1 } catch (e) { logErr('editions: пункт оглавления', e) }
    return { label: it.label, href: it.href, index }
  })
  const n = v.book?.sections?.length || 0
  return { chapters: buildChapters(items, n), sections: n }
}

function halfFormat(ed) {
  if (currentWork && ed.id === currentWork.id) return currentFormat || ed.file_format
  if (ed.file_format === 'pdf') return ed.converted ? 'epub' : 'pdf'
  return ed.file_format || 'epub'
}

async function loadHalf(i, ed, gen) {
  const host = $(`#split-body .col[data-col="${i}"] .col-body`)
  const old = S.views[i]
  if (old) { try { old.close() } catch (e) { logErr('editions: закрытие половины', e) } old.book?.destroy?.() }
  const v = document.createElement('foliate-view')
  host.replaceChildren(v)
  S.views[i] = v; S.last[i] = null; S.maps[i] = null; S.ready[i] = false
  let fmt = halfFormat(ed)
  const q = ed.file_format === 'pdf' ? (fmt === 'pdf' ? '?original=1' : '?format=epub') : ''
  const r = await fetch(`/api/reader/${ed.id}/file${q}`, { credentials: 'same-origin' })
  if (!r.ok) throw new Error(`файл издания ${ed.id}: HTTP ${r.status}`)
  fmt = r.headers.get('X-Book-Format') || fmt
  const blob = await r.blob()
  if (gen !== S.gen) return
  await v.open(new File([blob], `book.${fmt}`, { type: blob.type }))
  if (gen !== S.gen) return
  S.fmts[i] = fmt
  applyViewStyles(v, { work: ed, host })
  v.renderer.setAttribute('max-column-count', '1')
  v.renderer.setAttribute('max-inline-size', String(Math.max(240, host.clientWidth - 16)))
  // Индекс половины берём в момент события: «Поменять местами» переставляет их.
  const at = () => S.views.indexOf(v)
  v.renderer.addEventListener('relocate', (e) => { if (at() >= 0) onHalfRelocate(at(), e.detail) })
  v.addEventListener('load', (e) => {
    const doc = e.detail?.doc
    if (!doc) return
    for (const evName of ['pointerdown', 'wheel', 'touchstart', 'keydown']) {
      doc.addEventListener(evName, () => { if (at() >= 0) setLead(at()) }, { passive: true })
    }
    doc.addEventListener('click', (ce) => {
      const w = doc.documentElement?.clientWidth || 1
      const x = ce.clientX / w
      if (x > 0.25 && x < 0.75 && doc.getSelection?.().isCollapsed !== false) $('#split').classList.toggle('chrome-off')
    })
  })
  S.maps[i] = chaptersOf(v)
  const same = currentWork && ed.id === currentWork.id && view?.lastLocation?.cfi && fmt === currentFormat
  await v.init(same ? { lastLocation: view.lastLocation.cfi } : { showTextStart: true })
  setTimeout(() => { if (gen === S.gen && at() >= 0) S.ready[at()] = true }, 400)
}

function setLead(i) {
  if (S.lead === i) return
  S.lead = i
  paintTags()
}

function onHalfRelocate(i, detail) {
  S.last[i] = detail
  // Ведущую назначает только действие человека в половине (pointerdown/wheel/
  // touchstart/keydown — см. loadHalf), а не relocate: по таймерам ведомая
  // половина под нагрузкой успевала «стать ведущей», и половины перехватывали
  // ведущую друг у друга. Здесь — только «ведомая идёт следом за ведущей».
  if (S.syncing || !S.ready[i] || i !== S.lead) return
  if (S.sync) followLead()
}

let followTimer = null
function followLead() {
  clearTimeout(followTimer)
  followTimer = setTimeout(async () => {
    const i = S.lead, o = 1 - i
    const src = S.views[i], dst = S.views[o], at = S.last[i]
    if (!src || !dst || !at || !S.maps[o]) return
    S.syncing++
    try {
      const m = !src.isFixedLayout && !dst.isFixedLayout && S.maps[i]?.chapters.length && S.maps[o].chapters.length
        ? mapSection(S.maps[i], S.maps[o], at.index, at.fraction || 0) : null
      // PDF без оглавления/текста — только доля книги (страницы).
      if (m) await dst.renderer.goTo({ index: m.index, anchor: () => m.fraction })
      else await dst.goToFraction(src.lastLocation?.fraction || 0)
    } catch (e) {
      logErr('editions: синхронная прокрутка', e)
    } finally {
      // Ведомая половина отвечает своим relocate чуть позже — его не считаем «листанием».
      setTimeout(() => { S.syncing = Math.max(0, S.syncing - 1) }, 300)
    }
  }, 120)
}

function paintTags() {
  for (const i of [0, 1]) {
    const col = $(`#split-body .col[data-col="${i}"]`)
    if (!col) continue
    const ed = S.pair[i], lead = S.lead === i
    col.classList.toggle('is-lead', lead)
    const tag = col.querySelector('.tag')
    tag.innerHTML = `${lead ? '<i></i>' : ''}${escapeHtml(lang(ed))}·${escapeHtml(edName(ed))}`
    tag.setAttribute('aria-label', `Издание: ${edShort(ed)}${lead ? ', ведущее' : ''}`)
    tag.setAttribute('aria-expanded', String(S.menu === i))
    const menu = col.querySelector('.menu')
    menu.hidden = S.menu !== i
    if (S.menu === i) {
      menu.innerHTML = (group?.editions || []).map((x) => `
        <button class="mi" role="menuitem" type="button" data-pick="${x.id}" aria-current="${x.id === ed.id}">
          <span class="ed-badge lang">${escapeHtml(lang(x))}</span><span><b>${escapeHtml(edTitle(x))}</b><small>${escapeHtml((x.file_format || '').toUpperCase())} · ${pctOf(x)}%</small></span>${x.id === ed.id ? icon('check') : '<span></span>'}
        </button>`).join('') + `<div class="sep"></div>
        <button class="mi" role="menuitem" type="button" data-lead="1"><span>${icon('refresh')}</span><span><b>Сделать ведущей</b><small>${lead ? 'уже ведущая' : 'вторая половина пойдёт следом'}</small></span><span></span></button>`
    }
  }
  const rows = splitRows()
  $('#split-body').classList.toggle('rows', rows)
  const ob = $('#split-orient')
  ob.innerHTML = icon(rows ? 'columns' : 'rows')
  ob.title = rows ? 'Расположить в ряд' : 'Расположить друг под другом'
  ob.setAttribute('aria-label', ob.title)
  $('#split-sync').setAttribute('aria-pressed', String(S.sync))
}

function buildSplitDom() {
  $('#split-body').innerHTML = [0, 1].map((i) => `
    <div class="col" data-col="${i}">
      <button class="tag" type="button" aria-haspopup="menu" aria-expanded="false"></button>
      <div class="menu" role="menu" aria-label="Издание в этой половине" hidden></div>
      <div class="col-body"></div>
    </div>`).join('')
  for (const i of [0, 1]) {
    const col = $(`#split-body .col[data-col="${i}"]`)
    col.querySelector('.tag').addEventListener('click', () => { S.menu = S.menu === i ? null : i; paintTags() })
    col.querySelector('.menu').addEventListener('click', async (e) => {
      const pick = e.target.closest('[data-pick]'), lead = e.target.closest('[data-lead]')
      S.menu = null
      if (lead) { S.lead = i; paintTags(); followLead(); return }
      if (pick) {
        const ed = (group?.editions || []).find((x) => x.id === Number(pick.dataset.pick))
        if (ed && ed.id !== S.pair[i].id) {
          S.pair[i] = ed
          paintTags()
          try { await loadHalf(i, ed, S.gen); S.lead = 1 - i; followLead() } catch (err) {
            logErr('editions: смена издания в половине', err)
            toast('Издание не открылось: ' + err.message, 'err')
          }
        }
      }
      paintTags()
    })
  }
}

// pair — id двух изданий; lead — индекс ведущей половины.
export async function openSplit(pair, lead = 0) {
  if (!group || (group.editions || []).length < 2) return
  closeMenu(); hideToast(); hideChip()
  const eds = group.editions
  const a = eds.find((e) => e.id === pair?.[0]) || currentEd(group)
  const b = eds.find((e) => e.id === pair?.[1] && e.id !== a.id) || otherLanguage(group) || others(group)[0]
  if (!a || !b) return
  S.pair = [a, b]; S.lead = lead; S.menu = null; S.gen++
  const gen = S.gen
  document.body.classList.add('split-open')
  $('#split').hidden = false
  $('#split').classList.remove('chrome-off')
  buildSplitDom()
  paintTags()
  try {
    await Promise.all([loadHalf(0, a, gen), loadHalf(1, b, gen)])
    if (gen !== S.gen) return
    // Ведущая уже стоит на месте читалки — вторая встаёт следом.
    if (S.sync) followLead()
  } catch (e) {
    logErr('editions: разделённый экран', e)
    toast('Не удалось открыть два издания рядом: ' + e.message, 'err', 6000)
    closeSplit()
  }
}

function disposeSplit() {
  S.gen++
  for (const v of S.views) {
    if (!v) continue
    try { v.close() } catch (e) { logErr('editions: закрытие половины', e) }
    v.book?.destroy?.()
  }
  S.views = [null, null]
  $('#split-body').innerHTML = ''
  $('#split').hidden = true
  document.body.classList.remove('split-open')
}

export function closeSplit() { disposeSplit() }

// Выход: читалка остаётся на ведущем издании (на его месте в половине).
async function exitSplit() {
  const ed = S.pair[S.lead], v = S.views[S.lead], fmt = S.fmts[S.lead]
  const loc = v?.lastLocation
  if (!ed || !loc) { disposeSplit(); return }
  if (currentWork && ed.id === currentWork.id) {
    disposeSplit()
    if (fmt === currentFormat && loc.cfi) {
      try { await view?.goTo(loc.cfi) } catch (e) { logErr('editions: возврат в читалку', e) }
    }
    return
  }
  const pos = { ratio: loc.fraction || 0, locator: loc.cfi || '', text_anchor: captureAnchor(loc.range),
    chapter: loc.tocItem?.label || '', book_rev: '', file_format: fmt, at_end: false }
  disposeSplit()
  // Сперва место текущего издания, потом ведущего: ведущее — самое свежее, и
  // открыть его надо на его же месте, без переноса «из группы».
  await settlePosition()
  const ok = await commitPosition(ed.id, pos)
  if (!ok) toast('Место ведущего издания не сохранилось на сервере — открою его последнее место', 'err')
  await openEdition(ed, { ownPlace: true })
}

// ------------------------------------------------------ страница книги

// Блок «Издания» на странице книги. box — контейнер, work — книга страницы.
export function mountEditions(box, work, { onOpen } = {}) {
  const st = { data: null, linkOpen: false, q: '', unlink: null, sug: null, err: '' }
  const render = () => {
    if (!box.isConnected) return
    const d = st.data
    if (!d) { box.innerHTML = st.err ? `<div class="bp-section-h">Издания</div><p class="hint">Не загрузились: ${escapeHtml(st.err)}</p>` : ''; return }
    const eds = d.editions || []
    const many = eds.length > 1
    box.innerHTML = `
      <div class="sec-h"><div class="bp-section-h">Издания</div>
        <button class="btn-ghost ed-btn" type="button" data-act="linkp" aria-expanded="${st.linkOpen}">${icon('plus')} Связать с изданием</button></div>
      <p class="hint">Одно произведение в разных файлах. Прогресс у каждого свой.</p>
      <div class="ed-list">${eds.map((e) => row(e, many)).join('')}</div>
      ${many ? `<div class="sync-row"><div><b>Синхронизировать позицию</b><p class="hint">Открыл другое издание, оно встаёт на то же место.</p></div>
        <button class="sw" type="button" role="switch" aria-checked="${!!d.group?.sync}" aria-label="Синхронизировать позицию" data-act="sync"></button></div>` : ''}
      ${st.linkOpen ? panel() : ''}`
    const input = box.querySelector('#ed-q')
    if (input && st.focusQ) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); st.focusQ = false }
  }
  const row = (e, many) => `
    <div class="ed${e.current ? ' cur' : ''}">
      <button class="ed-badge lang" type="button" data-act="lang" data-id="${e.id}" title="Язык издания — нажмите, чтобы исправить" aria-label="Язык ${escapeHtml(lang(e))}, исправить">${escapeHtml(lang(e))}</button>
      <div><button class="ed-name" type="button" data-act="label" data-id="${e.id}" title="Метка издания — нажмите, чтобы изменить">${escapeHtml(edTitle(e))}</button>
        <div class="ed-meta"><span class="ed-badge">${icon('file-text', 14)} ${escapeHtml((e.file_format || '').toUpperCase())}</span>${e.current ? '<span class="ed-badge acc">Открыто сейчас</span>' : ''}</div></div>
      <div class="ed-act"><button class="btn-ghost" type="button" data-act="open" data-id="${e.id}">Читать</button>
        ${many ? `<button class="icon-btn" type="button" aria-label="Отвязать издание ${escapeHtml(edShort(e))}" data-act="unlink" data-id="${e.id}">${icon('link-off')}</button>` : ''}</div>
      <div class="ed-prog"><div class="track${e.finished || pctOf(e) >= 95 ? ' read' : ''}"><i style="width:${pctOf(e)}%"></i></div><span class="pct">${pctOf(e)}%</span><span class="dim">${escapeHtml(e.chapter || '')}</span></div>
      ${st.unlink === e.id ? `<div class="confirm"><span>Отвязать «${escapeHtml(edShort(e))}»? Книга и прогресс останутся.</span>
        <button class="btn-ghost btn-danger" type="button" data-act="unlink-ok" data-id="${e.id}">Отвязать</button>
        <button class="btn-ghost" type="button" data-act="unlink-no">Отмена</button></div>` : ''}
    </div>`
  const card = (c) => `<div class="sug-item"><div><div class="ed-name">${escapeHtml(c.title || 'Без названия')}</div>
      <div class="ed-meta">${c.language ? `<span class="ed-badge lang">${escapeHtml(c.language.toUpperCase())}</span>` : ''}<span class="ed-badge">${escapeHtml((c.file_format || '').toUpperCase())}</span>${c.author ? `<span class="dim">${escapeHtml(c.author)}</span>` : ''}</div></div>
      <button class="btn-ghost" type="button" data-act="link" data-id="${c.id}">${icon('link')} Связать</button></div>`
  const panel = () => {
    const s = st.sug
    const list = s ? (s.suggested.length + s.others.length) : -1
    return `<div class="ed-panel"><h3>Связать с другим изданием</h3>
      <label class="field">${icon('search')}<input id="ed-q" type="search" placeholder="Название или автор" value="${escapeHtml(st.q)}" aria-label="Поиск по библиотеке"></label>
      <div class="sug">
        ${!s ? '<p class="hint">Ищу похожие…</p>' : ''}
        ${s?.suggested.length ? `<div class="sug-tag">${icon('wand', 16)} Похоже на то же произведение</div>${s.suggested.map(card).join('')}` : ''}
        ${s?.others.length ? `<div class="dim sug-rest">Остальная библиотека</div>${s.others.map(card).join('')}` : ''}
        ${list === 0 ? '<p class="hint">Ничего не нашлось. Проверь написание или добавь книгу в библиотеку.</p>' : ''}
      </div>
      <div class="ed-panel-foot"><button class="btn-ghost" type="button" data-act="linkx">Закрыть</button></div></div>`
  }
  const load = async () => {
    try { st.data = await loadGroup(work.id); st.err = '' } catch (e) {
      logErr('editions: блок изданий', e)
      st.err = e.message || 'ошибка сети'
    }
    render()
  }
  let qTimer = null
  const loadSug = async () => {
    try { st.sug = await api.get(`/api/editions/${work.id}/suggest?q=${encodeURIComponent(st.q)}`) } catch (e) {
      logErr('editions: подсказки', e)
      st.sug = { suggested: [], others: [] }
      toast('Поиск изданий не удался: ' + e.message, 'err')
    }
    st.focusQ = true
    render()
  }
  const mutate = async (fn, what) => {
    try { st.data = await fn(); render() } catch (e) {
      logErr(`editions: ${what}`, e)
      toast(`Не удалось ${what}: ` + (typeof e.data?.detail === 'string' ? e.data.detail : e.message), 'err')
    }
  }
  box.addEventListener('input', (e) => {
    if (e.target.id !== 'ed-q') return
    st.q = e.target.value
    clearTimeout(qTimer)
    qTimer = setTimeout(loadSug, 250)
  })
  box.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-act]')
    if (!b) return
    const id = Number(b.dataset.id)
    const ed = (st.data?.editions || []).find((x) => x.id === id)
    switch (b.dataset.act) {
      case 'linkp': st.linkOpen = !st.linkOpen; render(); if (st.linkOpen) loadSug(); break
      case 'linkx': st.linkOpen = false; render(); break
      case 'link': st.linkOpen = false; await mutate(() => api.post(`/api/editions/${work.id}/link`, { other_id: id }), 'связать издания'); break
      case 'unlink': st.unlink = id; render(); break
      case 'unlink-no': st.unlink = null; render(); break
      case 'unlink-ok':
        st.unlink = null
        await mutate(async () => { await api.delete(`/api/editions/${id}/link`); return loadGroup(work.id) }, 'отвязать издание')
        break
      case 'sync': await mutate(() => api.put(`/api/editions/${work.id}/sync`, { sync: !st.data?.group?.sync }), 'переключить синхронизацию'); break
      case 'open': onOpen?.(id); break
      case 'label': {
        const v = prompt('Метка издания («перевод Катаева», «оригинал»):', ed?.edition_label || '')
        if (v != null) await mutate(() => api.put(`/api/editions/${id}/meta`, { edition_label: v }).then(() => loadGroup(work.id)), 'сохранить метку')
        break
      }
      case 'lang': {
        const v = prompt('Язык издания (ru, en, uk…). Пусто — определить по книге:', ed?.language || '')
        if (v != null) await mutate(() => api.put(`/api/editions/${id}/meta`, { language: v }).then(() => loadGroup(work.id)), 'сохранить язык')
        break
      }
    }
  })
  box.classList.add('ed-sec')
  load()
}

// --------------------------------------------------------------- проводка

function wire() {
  $('#ed-switch')?.addEventListener('click', (e) => {
    e.stopPropagation()
    if ($('#ed-menu').hidden) openMenu(); else closeMenu()
  })
  $('#ed-quick')?.addEventListener('click', () => {
    const id = Number($('#ed-quick').dataset.target)
    if (id) openEdition(id)
  })
  $('#split-btn').innerHTML = icon('columns')
  $('#split-btn')?.addEventListener('click', () => openSplit([currentWork?.id], 0))
  $('#ed-menu')?.addEventListener('click', async (e) => {
    const it = e.target.closest('.mi')
    if (!it) return
    if (it.dataset.ed) {
      const id = Number(it.dataset.ed)
      closeMenu()
      if (id !== currentWork?.id) openEdition(id)
      return
    }
    if (it.dataset.act === 'split') { closeMenu(); openSplit([currentWork?.id], 0); return }
    if (it.dataset.act === 'sync') { await toggleSync(); openMenu() }
  })
  document.addEventListener('click', (e) => {
    if (!e.target.closest?.('#ed-menu, #ed-switch')) closeMenu()
    if (S.menu != null && !e.target.closest?.('#split-body .col')) { S.menu = null; paintTags() }
  })
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return
    if (!$('#ed-menu').hidden) { closeMenu(); $('#ed-switch')?.focus() }
    else if (!$('#split').hidden) exitSplit()
  })
  // Широкий экран: частые действия «Ещё» — иконками в тулбаре; жмут те же кнопки.
  const wide = { '#tts-wide': ['#tts-btn', 'volume'], '#tr-wide': ['#translate-btn', 'language'],
    '#bm-wide': ['#bm-btn', 'bookmark'], '#hl-wide': ['#hl-btn', 'highlight'] }
  for (const [id, [target, ic]] of Object.entries(wide)) {
    const b = $(id)
    if (!b) continue
    b.innerHTML = icon(ic)
    b.addEventListener('click', () => $(target)?.click())
  }
  // Состояние перевода и озвучивания видно на самих иконках.
  const mirror = (src, dst) => {
    const s = $(src), d = $(dst)
    if (!s || !d) return
    const sync = () => {
      d.setAttribute('aria-pressed', s.getAttribute('aria-pressed') || 'false')
      d.classList.toggle('tts-active', s.classList.contains('tts-active'))
      const lbl = s.querySelector('.more-label')?.textContent
      if (lbl) { d.title = lbl; d.setAttribute('aria-label', lbl) }
    }
    new MutationObserver(sync).observe(s, { attributes: true, subtree: true, childList: true, characterData: true })
    sync()
  }
  mirror('#translate-btn', '#tr-wide')
  mirror('#tts-btn', '#tts-wide')
  // «Ещё» на широком экране нужен только ради «Проверить новые главы».
  const upd = $('#update-btn'), more = $('#more-btn')
  if (upd && more) {
    const paintMore = () => more.classList.toggle('more-has-update', !upd.hidden)
    new MutationObserver(paintMore).observe(upd, { attributes: true, attributeFilter: ['hidden'] })
    paintMore()
  }
  $('#split-exit').innerHTML = icon('x')
  $('#split-sync').innerHTML = icon('refresh')
  $('#split-swap').innerHTML = icon('exchange')
  $('#split-exit').addEventListener('click', exitSplit)
  $('#split-sync').addEventListener('click', () => { S.sync = !S.sync; paintTags(); if (S.sync) followLead() })
  $('#split-swap').addEventListener('click', () => {
    // Меняем местами половины целиком (view вместе с местом), без перезагрузки.
    const body = $('#split-body')
    const [c0, c1] = [...body.children]
    if (!c0 || !c1) return
    body.insertBefore(c1, c0)
    c0.dataset.col = '1'; c1.dataset.col = '0'
    for (const k of ['pair', 'views', 'maps', 'last', 'fmts', 'ready']) S[k] = [S[k][1], S[k][0]]
    S.lead = 1 - S.lead
    paintTags()
  })
  $('#split-orient').addEventListener('click', () => {
    S.orient = splitRows() ? 'cols' : 'rows'
    paintTags()
    relayoutHalves()
  })
  let rz = null
  window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(() => { if (!$('#split').hidden) { paintTags(); relayoutHalves() } }, 200) })
  document.addEventListener('reader-opened', (e) => { onReaderOpened(e).catch((err) => logErr('editions: открытие издания', err)) })
}

function relayoutHalves() {
  for (const i of [0, 1]) {
    const v = S.views[i]
    const host = $(`#split-body .col[data-col="${i}"] .col-body`)
    if (!v?.renderer || !host) continue
    v.renderer.setAttribute('max-inline-size', String(Math.max(240, host.clientWidth - 16)))
  }
}

wire()
