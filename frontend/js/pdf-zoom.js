// Масштаб PDF (serg/tasks#3274).
//
// Раньше лента PDF всегда растягивала страницу на всю ширину окна — на большом
// мониторе это огромные буквы. Теперь масштаб выбирает человек:
//  * «По ширине окна» (как было в ленте), «Страница целиком», проценты;
//  * кнопки − / + и список в верхней панели — только у PDF и книг-макетов;
//  * Ctrl + колесо, Ctrl + / Ctrl − / Ctrl 0, щипок двумя пальцами.
// 100% — натуральный размер страницы PDF, как в Acrobat (1 pt = 4/3 CSS px).
// Масштаб помнится для книги на этом устройстве (localStorage): на телефоне и
// на мониторе у одной книги он свой, на сервер не уходит.
import { $ } from './core/dom.js'
import { view, currentWork } from './core/state.js'
import { logErr } from './core/log.js'

const KEY = 'reader.pdfZoom.v1'
const MIN = 0.5
const MAX = 3
const PRESETS = [0.75, 1, 1.25, 1.5]

let zoom = null    // null — по умолчанию режима; 'fit-width' | 'fit-page' | число (1 = 100%)
let isPdf = false

function loadAll() {
  try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {} } catch (e) {
    // Нет хранилища (приватное окно) — масштаб просто не запомнится.
    logErr('масштаб PDF: не прочитал сохранённое', e)
    return {}
  }
}
function saveZoom() {
  const id = currentWork?.id
  if (!id) return
  const all = loadAll()
  if (zoom == null) delete all[id]
  else all[id] = zoom
  try { localStorage.setItem(KEY, JSON.stringify(all)) } catch (e) {
    logErr('масштаб PDF: не сохранил', e)
  }
}

const renderer = () => (view?.isFixedLayout ? view.renderer : null)
// Единицы страницы → CSS px при 100%: у PDF страница в пунктах.
const unit = () => (isPdf ? 4 / 3 : 1)
const clamp = (z) => Math.min(MAX, Math.max(MIN, Math.round(z * 100) / 100))

// Масштаб, которым страница показана сейчас, в долях от 100%.
function effective() {
  const s = renderer()?.currentScale
  return s ? s / unit() : 1
}

function label() {
  const r = renderer()
  if (zoom === 'fit-width' || (zoom == null && r?.scrolled)) return 'По ширине'
  if (zoom === 'fit-page' || zoom == null) return 'Страница'
  return `${Math.round(zoom * 100)}%`
}

function paint() {
  const box = $('#pdf-zoom')
  if (!box) return
  const r = renderer()
  box.hidden = !r
  // На узком экране кнопок нет (место — тексту, масштаб там щипком); число
  // остаётся, только если масштаб не по умолчанию: по нему список и сброс.
  box.classList.toggle('pdf-zoom-custom', zoom != null)
  $('#pdf-zoom-val').textContent = label()
  for (const it of document.querySelectorAll('#pdf-zoom-menu [data-z]')) {
    it.setAttribute('aria-checked', String(String(it.dataset.z) === String(zoom ?? (r?.scrolled ? 'fit-width' : 'fit-page'))))
  }
}

let applyTimer = null
function apply({ now = true } = {}) {
  const r = renderer()
  if (!r) return
  const run = () => {
    if (zoom == null) r.removeAttribute('zoom')
    else r.setAttribute('zoom', typeof zoom === 'number' ? String(zoom * unit()) : zoom)
    paint()
  }
  clearTimeout(applyTimer)
  // Колесо шлёт десятки событий: число в панели меняем сразу, а перерисовку
  // страниц (дорогой рендер PDF) — раз, когда колесо остановилось.
  if (now) run(); else { paint(); applyTimer = setTimeout(run, 140) }
}

export function setZoom(z, opts) {
  zoom = typeof z === 'number' ? clamp(z) : z
  saveZoom()
  apply(opts)
}

function step(dir) {
  const base = typeof zoom === 'number' ? zoom : effective()
  // Шаг 10% от ближайшей «круглой» отметки: 87% → 90% / 80%, а не 97% / 77%.
  const next = dir > 0 ? Math.floor(base * 10 + 1e-6) / 10 + 0.1 : Math.ceil(base * 10 - 1e-6) / 10 - 0.1
  setZoom(next)
}

function onZoomRequest(e) {
  const d = e.detail || {}
  const base = typeof zoom === 'number' ? zoom : effective()
  if (d.ratio) setZoom(base * d.ratio)
  else if (d.wheel) {
    zoom = clamp(base * Math.exp(-d.wheel * 0.001))
    saveZoom()
    apply({ now: false })
  }
}

function onKey(e) {
  if (!renderer() || !(e.ctrlKey || e.metaKey) || e.altKey) return
  if (e.key === '+' || e.key === '=') { e.preventDefault(); step(1) }
  else if (e.key === '-' || e.key === '_') { e.preventDefault(); step(-1) }
  else if (e.key === '0') { e.preventDefault(); setZoom(null) }
}

// ---------- меню масштаба ----------
function closeMenu() {
  const m = $('#pdf-zoom-menu')
  if (m && !m.hidden) { m.hidden = true; $('#pdf-zoom-val')?.setAttribute('aria-expanded', 'false') }
}
function openMenu() {
  const m = $('#pdf-zoom-menu')
  const btn = $('#pdf-zoom-val')
  if (!m || !btn) return
  paint()
  m.hidden = false
  btn.setAttribute('aria-expanded', 'true')
  const r = btn.getBoundingClientRect()
  const w = m.offsetWidth
  m.style.top = `${Math.round(r.bottom + 6)}px`
  m.style.left = `${Math.round(Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2)))}px`
  m.querySelector('[aria-checked="true"]')?.focus()
}

function buildMenu() {
  const m = $('#pdf-zoom-menu')
  if (!m || m.childElementCount) return
  const item = (z, text) => {
    const b = document.createElement('button')
    b.type = 'button'; b.className = 'pdf-zoom-item'; b.setAttribute('role', 'menuitemradio')
    b.dataset.z = String(z); b.textContent = text
    b.addEventListener('click', () => { closeMenu(); setZoom(z) })
    return b
  }
  m.append(item('fit-width', 'По ширине окна'), item('fit-page', 'Страница целиком'))
  m.append(Object.assign(document.createElement('hr'), { className: 'pdf-zoom-sep' }))
  for (const z of PRESETS) m.append(item(z, `${Math.round(z * 100)}%`))
  const hint = document.createElement('div')
  hint.className = 'pdf-zoom-hint'
  hint.textContent = 'Ctrl + колесо, Ctrl ± и Ctrl 0 · на телефоне — щипок'
  m.append(hint)
}

const hooked = new WeakSet()
function hookView(v) {
  const r = v?.isFixedLayout ? v.renderer : null
  if (!r || hooked.has(r)) return
  hooked.add(r)
  r.addEventListener('zoom-request', onZoomRequest)
  // Клавиши внутри страницы (iframe) до окна не доходят.
  v.addEventListener('load', (e) => e.detail?.doc?.addEventListener('keydown', onKey))
  for (const c of r.getContents?.() || []) c.doc?.addEventListener('keydown', onKey)
  // Ctrl + колесо над полями вокруг страницы (вне iframe).
  r.addEventListener('wheel', (e) => {
    if (!(e.ctrlKey || e.metaKey)) return
    e.preventDefault()
    onZoomRequest({ detail: { wheel: e.deltaY } })
  }, { passive: false })
}

export function initPdfZoom() {
  buildMenu()
  $('#pdf-zoom-in')?.addEventListener('click', () => step(1))
  $('#pdf-zoom-out')?.addEventListener('click', () => step(-1))
  $('#pdf-zoom-val')?.addEventListener('click', () => ($('#pdf-zoom-menu')?.hidden ? openMenu() : closeMenu()))
  document.addEventListener('pointerdown', (e) => {
    if (!e.target.closest?.('#pdf-zoom-menu, #pdf-zoom-val')) closeMenu()
  })
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeMenu()
    onKey(e)
  })
  window.addEventListener('resize', closeMenu)
  document.addEventListener('reader-opening', () => { zoom = null; isPdf = false; closeMenu(); paint() })
  document.addEventListener('reader-opened', (e) => {
    const { work, fmt, view: v } = e.detail || {}
    isPdf = fmt === 'pdf'
    zoom = v?.isFixedLayout ? (loadAll()[work?.id] ?? null) : null
    hookView(v)
    apply()
  })
  // Сменили «Ленту» на «Страницы»: подпись «по умолчанию» у режимов разная.
  document.addEventListener('reader-format-change', paint)
  $('#flow-mode')?.addEventListener('change', () => setTimeout(paint, 0))
}

initPdfZoom()
