// История переходов по книге: кнопка «Назад» в читалке и список позиций по
// долгому нажатию (serg/tasks#1078, переписано в serg/tasks#3239).
//
// Переход решает только клиент, по явному действию человека: оглавление,
// сноска, поиск, закладка, шкала, глава ±1 не с последней страницы главы,
// Ctrl+Home/End, клик в этой панели, открытие на главе со страницы книги.
// Все они идут через navigate(reason, go) — см. core/jump-stack.js.
// Обычное листание, в том числе через границу главы, — не переход.
//
// Две памяти:
//  * локальный стек этой вкладки — «Назад» без сети, LIFO;
//  * серверная история (/api/progress/{id}/history) — переживает
//    перезагрузку и видна с другого устройства. Туда же сервер сам кладёт
//    место, сдвинутое другой вкладкой или устройством (backend/app/routers/progress.py).
import { $, escapeHtml, toast } from './core/dom.js'
import { api } from './core/api.js'
import { logErr } from './core/log.js'
import { view, currentWork } from './core/state.js'
import { gotoPosition, captureAnchor } from './core/position.js'
import { createJumpStack, isEmptyPos } from './core/jump-stack.js'
import { identity, deviceId } from './core/device.js'

let serverHasRows = false  // знает ли сервер о прошлых позициях этой книги
let bookRevNow = ''        // SHA-1 открытого файла: возврат по CFI точен только в нём
let formatNow = ''

// Текущая позиция живой читалки в том виде, в каком её хранит сервер.
function currentPosition() {
  const loc = view?.lastLocation
  if (!loc) return null
  const me = identity()
  return {
    ratio: loc.fraction || 0,
    locator: loc.cfi || '',
    text_anchor: captureAnchor(loc.range) || '',
    chapter: loc.tocItem?.label || '',
    book_rev: bookRevNow,
    device: me.device,
    device_id: me.device_id,
  }
}

// Дождаться relocate после перехода. goTo у foliate обычно эмитит его внутри
// себя; таймаут — на переход «в то же место», после которого relocate нет.
function settle(ms = 700) {
  const v = view
  if (!v) return Promise.resolve()
  return new Promise((resolve) => {
    let t = null
    const done = () => { clearTimeout(t); v.removeEventListener('relocate', on); resolve() }
    const on = () => setTimeout(done, 60)
    v.addEventListener('relocate', on)
    t = setTimeout(done, ms)
  })
}

const jumps = createJumpStack({
  current: currentPosition,
  settle,
  goTo: async (pos) => {
    if (!view) return false
    const ok = await gotoPosition(view, pos, bookRevNow, formatNow)
    if (!ok) toast('Это место в книге больше не находится — возможно, её пересобрали', 'err')
    return ok
  },
  post: (row) => {
    const id = currentWork?.id
    if (!id) return null
    serverHasRows = true
    syncBackBtn()
    return api.post(`/api/progress/${id}/history`, row)
  },
  loadRows: () => {
    const id = currentWork?.id
    return id ? api.get(`/api/progress/${id}/history`) : []
  },
  newId: () => (globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`),
  onChange: () => syncBackBtn(),
  onError: (what, e) => logErr(what, e),
})

// Кнопка «Назад» активна, только когда есть куда возвращаться.
function syncBackBtn() {
  const btn = $('#jump-back-btn')
  if (!btn) return
  const can = jumps.size > 0 || (serverHasRows && !jumps.seeded)
  btn.disabled = !can
  btn.title = can
    ? 'Вернуться к прошлой позиции (удерживайте — список переходов)'
    : 'Переходов пока не было'
}

// Вызывается при открытии книги: стек чужой книги к новой отношения не имеет.
export async function resetJumps(workId, { bookRev = '', format = '' } = {}) {
  jumps.reset()
  bookRevNow = bookRev || ''
  formatNow = format || ''
  serverHasRows = false
  syncBackBtn()
  if (!workId) return
  try {
    const rows = await api.get(`/api/progress/${workId}/history`)
    serverHasRows = Array.isArray(rows) && rows.length > 0
  } catch (e) {
    // Нет связи — локальный стек всё равно работает, но молчать нельзя:
    // иначе «Назад» выглядит сломанным без объяснения.
    logErr('история переходов не загрузилась', e)
  }
  syncBackBtn()
}

// Единственная точка входа для переходов. reason попадает в подпись строки.
export function navigate(reason, go, opts) {
  if (!view) return Promise.resolve('failed')
  return jumps.navigate(reason, go, opts)
}

// Открыли книгу сразу на главе: исход — серверная позиция, на экране её не было.
export function rememberOrigin(pos, reason = 'open') {
  if (isEmptyPos(pos)) return null
  const me = identity()
  return jumps.remember({
    ratio: pos.ratio || 0, locator: pos.locator || '', text_anchor: pos.text_anchor || '',
    chapter: pos.chapter || '', book_rev: pos.book_rev || '',
    device: pos.device || me.device, device_id: pos.device_id || me.device_id,
  }, reason)
}

// Вкладка устарела и её не трогали: переходим на свежее место, своё кладём
// в локальный стек (на сервер не пишем — это место и так было серверной
// позицией, а у сервера оно уже в истории или продолжено) и говорим, откуда.
export async function followRemote(prog) {
  const reason = prog.device_id && prog.device_id === deviceId() ? 'tab' : 'device'
  const res = await jumps.navigate(reason, () => gotoPosition(view, prog, bookRevNow, formatNow), { post: false })
  if (res !== 'failed') toast(`Продолжаю с ${prog.device || 'другого устройства'}`, 'info', 4000)
  return res
}

export async function jumpBack() {
  const target = await jumps.back()
  if (!target) toast('Возвращаться некуда — переходов по этой книге не было', 'info')
  return target
}

const REASONS = {
  toc: 'оглавление',
  link: 'сноска',
  search: 'поиск',
  seek: 'шкала',
  bookmark: 'закладка',
  open: 'открыто с главы',
  chapter: 'следующая глава',
  edge: 'край книги',
  jumps: 'список переходов',
  tab: 'другая вкладка',
  offline: 'офлайн-досылка',
  merge: 'слияние дублей',
  readera: 'ReadEra',
  // Старые записи до serg/tasks#3239: сервер угадывал «затирание» по доле и
  // ставил его на каждую границу главы — это просто прежнее место.
  overwrite: 'прежнее место',
  back: 'возврат',
  jump: 'переход',
}

export function reasonLabel(r) {
  if (r.reason === 'device') return `другое устройство: ${r.device || 'неизвестное'}`
  return REASONS[r.reason] || r.reason || ''
}

function fmtWhen(iso) {
  if (!iso) return ''
  // Сервер отдаёт время в UTC, но без суффикса Z (naive datetime) — без него
  // браузер прочитал бы его как местное и показал «3 часа назад» как «сейчас».
  const norm = /[Z+]|-\d\d:\d\d$/.test(iso) ? iso : iso + 'Z'
  const d = new Date(norm)
  if (isNaN(d)) return ''
  const mins = Math.round((Date.now() - d.getTime()) / 60000)
  if (mins < 1) return 'только что'
  if (mins < 60) return `${mins} мин назад`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours} ч назад`
  return d.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

// Список для панели: локальные прыжки + серверная история, без дублей.
async function jumpList() {
  const id = currentWork?.id
  let rows = []
  if (id) {
    try {
      rows = (await api.get(`/api/progress/${id}/history`)) || []
    } catch (e) {
      logErr('история переходов не загрузилась', e)
    }
  }
  const seen = new Set()
  const all = []
  for (const r of jumps.entries().reverse().concat(rows)) {
    if (r.reason === 'back') continue
    const key = r.client_id || `${r.locator}|${r.text_anchor}|${Math.round((r.ratio || 0) * 1000)}`
    const place = `${r.locator}|${r.text_anchor}|${Math.round((r.ratio || 0) * 1000)}`
    if (seen.has(key) || seen.has(place)) continue
    seen.add(key); seen.add(place)
    all.push(r)
  }
  return all
}

export async function renderJumpsPanel() {
  const list = $('#jumps-list')
  if (!list) return
  list.innerHTML = '<div class="jumps-empty">Загружаю…</div>'
  const rows = await jumpList()
  if (!rows.length) {
    list.innerHTML = '<div class="jumps-empty">Переходов по этой книге ещё не было.'
      + ' Сюда попадают места, из которых вы ушли: по оглавлению, по сноске,'
      + ' поиском, шкалой — и место, где читали в другой вкладке или на другом устройстве.</div>'
    return
  }
  list.innerHTML = ''
  for (const r of rows) {
    const a = document.createElement('a')
    a.href = '#'
    a.className = 'jump-item'
    const pct = Math.round((r.ratio || 0) * 100)
    const where = r.device && r.reason !== 'device' ? ' · ' + escapeHtml(r.device) : ''
    a.innerHTML = `<span class="jump-ch">${escapeHtml(r.chapter || 'Без главы')}</span>`
      + `<span class="jump-meta">${pct}% · ${escapeHtml(reasonLabel(r))}${where}`
      + `${r.created_at ? ' · ' + escapeHtml(fmtWhen(r.created_at)) : ''}</span>`
    a.addEventListener('click', async (ev) => {
      ev.preventDefault()
      const { closePanels } = await import('./navigation.js')
      closePanels()
      if (isEmptyPos(r)) {
        toast('Эта запись без координат — переходить некуда', 'err')
        return
      }
      // Уход из текущего места по списку — тоже переход.
      await navigate('jumps', () => gotoPosition(view, r, bookRevNow, formatNow))
    })
    list.append(a)
  }
}

export async function clearJumps() {
  const id = currentWork?.id
  if (id) {
    try {
      await api.delete(`/api/progress/${id}/history`)
    } catch (e) {
      logErr('не очистил историю переходов', e)
      toast('Не удалось очистить историю', 'err')
      return
    }
  }
  jumps.reset()
  serverHasRows = false
  syncBackBtn()
  renderJumpsPanel()
}

// Кнопка в тулбаре: короткое нажатие — назад, долгое — список переходов.
const LONG_PRESS_MS = 520
const MOVE_TOL = 12

export function initJumps() {
  const btn = $('#jump-back-btn')
  if (!btn) return
  let timer = null
  let longFired = false
  let sx = 0
  let sy = 0

  const openList = async () => {
    longFired = true
    const { openPanel } = await import('./navigation.js')
    openPanel('#jumps-panel')
    renderJumpsPanel()
  }
  const cancel = () => { clearTimeout(timer); timer = null }

  btn.addEventListener('pointerdown', (e) => {
    if (btn.disabled) return
    longFired = false
    sx = e.clientX; sy = e.clientY
    cancel()
    timer = setTimeout(openList, LONG_PRESS_MS)
  })
  btn.addEventListener('pointermove', (e) => {
    if (!timer) return
    if (Math.abs(e.clientX - sx) > MOVE_TOL || Math.abs(e.clientY - sy) > MOVE_TOL) cancel()
  })
  btn.addEventListener('pointerup', cancel)
  btn.addEventListener('pointercancel', () => { cancel(); longFired = false })
  btn.addEventListener('pointerleave', cancel)
  // Контекстное меню на долгий тап в мобильном Chrome перехватило бы жест.
  btn.addEventListener('contextmenu', (e) => { if (longFired) e.preventDefault() })
  btn.addEventListener('click', (e) => {
    e.preventDefault()
    if (longFired) { longFired = false; return }
    jumpBack()
  })
  // Клавиатура: список переходов без долгого нажатия — Alt+стрелка вниз.
  btn.addEventListener('keydown', (e) => {
    if (e.altKey && e.key === 'ArrowDown') { e.preventDefault(); openList() }
  })
  $('#jumps-clear')?.addEventListener('click', clearJumps)
}
