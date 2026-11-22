// Надёжная запись позиции чтения (serg/tasks#1853).
//
// Раньше позиция уходила только PUT'ом из дебаунса (900 мс) и при закрытии
// читалки кнопкой. Свернули PWA, закрыли вкладку, пропала сеть — последнее место
// терялось, а упавший PUT просто писался в лог. Теперь:
//   1) каждая позиция СНАЧАЛА ложится в localStorage (синхронно — переживает
//      убийство вкладки), потом уходит на сервер;
//   2) ответ сервера снимает её из очереди, только если это та же запись;
//   3) неотправленное досылается при старте, при появлении сети и при открытии
//      книги — с флагом replay: сервер примет её, лишь если она новее своей.
//   4) (serg/tasks#3239) запись несёт, кто пишет (session_id, device_id, имя
//      устройства) и base_rev — ревизию сервера, от которой читали. По ним
//      сервер сам понимает, что место сдвинули в другой вкладке или на другом
//      устройстве, и кладёт прежнее в историю — без угадывания по доле.
import { logErr } from './log.js'
import { identity } from './device.js'

const KEY = 'reader.pendingProgress.v1'

// Ревизия серверной позиции, которую знает ЭТА вкладка: из GET при открытии,
// из ответов PUT, из повторного GET при возврате во вкладку.
const knownRevs = new Map()
export function knownRev(workId) {
  const v = knownRevs.get(String(workId))
  return v == null ? null : v
}
export function noteRev(workId, rev) {
  if (typeof rev === 'number') knownRevs.set(String(workId), rev)
}

function readAll() {
  try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {} } catch (e) { logErr('pending progress read', e); return {} }
}
function writeAll(all) {
  try { localStorage.setItem(KEY, JSON.stringify(all)) } catch (e) { logErr('pending progress write', e) }
}

// Неотправленная позиция книги: { body, saved_at } или null.
export function pendingFor(workId) {
  return readAll()[workId] || null
}

function drop(workId, savedAt) {
  const all = readAll()
  if (all[workId] && all[workId].saved_at === savedAt) {
    delete all[workId]
    writeAll(all)
  }
}

// Записать позицию: очередь → PUT. keepalive — для pagehide/visibilitychange:
// такой запрос браузер доводит до сервера и после выгрузки страницы.
export function commitPosition(workId, pos, { keepalive = false, replay = false, savedAt } = {}) {
  const saved_at = savedAt || Date.now()
  // Досылка несёт опознание и базу того раза, когда позицию прочитали.
  const body = replay && pos.session_id
    ? { ...pos, saved_at, replay }
    : { ...identity(), base_rev: knownRev(workId), ...pos, saved_at, replay }
  const all = readAll()
  all[workId] = { body, saved_at }
  writeAll(all)
  return send(workId, body, keepalive)
}

async function send(workId, body, keepalive) {
  try {
    const r = await fetch(`/api/progress/${workId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      keepalive,
      credentials: 'same-origin',
    })
    if (r.status === 409) {
      const conflict = await r.json()
      drop(workId, body.saved_at)
      document.dispatchEvent(new CustomEvent('reader-revision-conflict', { detail: { workId, ...conflict } }))
      return false
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    drop(workId, body.saved_at)
    // keepalive-ответ после выгрузки страницы читать уже некому.
    let out = null
    try { out = await r.json() } catch (e) { logErr('save progress: ответ не JSON, ревизия не обновлена', e) }
    // Отклонённая досылка (applied=false) ревизию не двигает: эта вкладка
    // по-прежнему не видела серверного места, и её следующая запись должна
    // положить его в историю, а не затереть молча.
    if (out && out.applied !== false) noteRev(workId, out.rev)
    document.dispatchEvent(new CustomEvent('reader-progress-saved', { detail: { workId, progress: out } }))
    return true
  } catch (e) {
    logErr('save progress (осталась в очереди)', e)
    return false
  }
}

// Дослать всё, что не ушло. Позиция в очереди старше той, что уже на сервере,
// там и отбросится (replay).
export async function replayPending() {
  const all = readAll()
  for (const [workId, rec] of Object.entries(all)) {
    if (!rec?.body) { drop(workId, rec?.saved_at); continue }
    await send(workId, { ...rec.body, replay: true }, false)
  }
}

// Время позиции на сервере в мс (клиентские часы того, кто её записал; для
// старых строк без saved_at — серверное время чтения).
export function serverSavedAt(prog) {
  if (!prog) return 0
  if (prog.saved_at) return Number(prog.saved_at)
  const t = prog.last_read_time
  if (!t) return 0
  return Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(t) ? t : t + 'Z') || 0
}

window.addEventListener('online', () => { replayPending() })
