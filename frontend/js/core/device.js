// Кто пишет позицию чтения (serg/tasks#3239).
//
//  * device_id — UUID устройства (браузера) в localStorage: по нему сервер
//    отличает «другую вкладку этого же устройства» от «другого устройства»;
//  * имя устройства — для людей, собирается автоматически: браузер имени
//    машины не отдаёт, а все устройства ходят с одного домашнего IP. Слои:
//    Client Hints (модель Android, версия Windows) → разбор UA → хвост id;
//  * session_id — случайный id ВКЛАДКИ, только в памяти страницы. Не в
//    sessionStorage: тот копируется при дублировании вкладки, и две вкладки
//    считались бы одной.
import { logErr } from './log.js'

const KEY = 'reader.device.v1'

// Коды моделей Android → торговые имена. Неизвестный код показываем как есть:
// «SM-A546B» понятнее, чем «Android».
const MODELS = [
  [/^SM-S918/i, 'Galaxy S23 Ultra'],
  [/^SM-S916/i, 'Galaxy S23+'],
  [/^SM-S911/i, 'Galaxy S23'],
  [/^SM-F936/i, 'Galaxy Z Fold4'],
  [/^SM-F946/i, 'Galaxy Z Fold5'],
  [/^(M2102J20SG|M2007J20CG)$/i, 'POCO X3'],
]

export function modelName(code) {
  const c = String(code || '').trim()
  if (!c) return ''
  for (const [re, name] of MODELS) if (re.test(c)) return name
  return c
}

function uuid() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  // Небезопасный контекст (http по IP) — randomUUID нет; уникальности хватает.
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (ch) => {
    const r = (Math.random() * 16) | 0
    return (ch === 'x' ? r : (r & 0x3) | 0x8).toString(16)
  })
}

function browserName(ua) {
  if (/YaBrowser\//.test(ua)) return 'Яндекс'
  if (/Edg(A|iOS)?\//.test(ua)) return 'Edge'
  if (/OPR\//.test(ua)) return 'Opera'
  if (/Firefox\/|FxiOS\//.test(ua)) return 'Firefox'
  if (/SamsungBrowser\//.test(ua)) return 'Samsung'
  if (/Chrome\/|CriOS\//.test(ua)) return 'Chrome'
  if (/Safari\//.test(ua)) return 'Safari'
  return 'Браузер'
}

// Имя по UA и (если есть) высокоэнтропийным Client Hints. Чистая функция —
// её проверяет tests/js/test_device.mjs.
export function deviceName({ ua = '', hints = null, id = '' } = {}) {
  const browser = browserName(ua)
  const tail = String(id || '').replace(/-/g, '').slice(0, 4)
  let model = ''
  let os = ''
  const platform = hints?.platform || ''
  if (platform === 'Android' || /Android/.test(ua)) {
    model = modelName(hints?.model)
    if (!model) {
      // «Android 10; K» — урезанный UA Chrome: модели в нём нет.
      const m = ua.match(/Android [\d.]+; (?:[a-z]{2}-[a-z]{2}; )?([^;)]+?)(?: Build\/[^;)]*)?\)/i)
      if (m && m[1] && m[1] !== 'K') model = modelName(m[1])
    }
    os = 'Android'
  } else if (/iPhone/.test(ua)) { model = 'iPhone' }
  else if (/iPad/.test(ua)) { model = 'iPad' }
  else if (platform === 'Windows' || /Windows/.test(ua)) {
    const major = parseInt(String(hints?.platformVersion || '').split('.')[0], 10)
    os = Number.isFinite(major) ? (major >= 13 ? 'Windows 11' : 'Windows 10') : 'Windows'
  } else if (platform === 'macOS' || /Macintosh|Mac OS X/.test(ua)) os = 'Mac'
  else if (platform === 'Chrome OS' || /CrOS/.test(ua)) os = 'ChromeOS'
  else if (platform === 'Linux' || /Linux/.test(ua)) os = 'Linux'
  else os = 'Устройство'
  // Известная модель различает устройство сама; одинаковые ПК — только хвостом id.
  if (model) return `${model} · ${browser}`
  return tail ? `${os} · ${browser} · ${tail}` : `${os} · ${browser}`
}

function readStored() {
  try { return JSON.parse(localStorage.getItem(KEY) || 'null') } catch (e) {
    logErr('device: не прочитал id устройства из localStorage', e)
    return null
  }
}
function store(rec) {
  try { localStorage.setItem(KEY, JSON.stringify(rec)) } catch (e) {
    // Приватный режим / заблокированное хранилище: id живёт до перезагрузки,
    // и сервер будет видеть эту вкладку новым устройством. Это видно в логе.
    logErr('device: не сохранил id устройства (будет новым после перезагрузки)', e)
  }
}

const stored = readStored()
let rec = stored && stored.id ? stored : { id: uuid(), name: '' }
if (!rec.name) {
  rec.name = deviceName({ ua: globalThis.navigator?.userAgent || '', id: rec.id })
  store(rec)
}

export const sessionId = uuid()
export function deviceId() { return rec.id }
export function deviceLabel() { return rec.name }
export function identity() { return { session_id: sessionId, device_id: rec.id, device: rec.name } }

// Уточнить имя по Client Hints (только Chromium, асинхронно). Считается один
// раз: имя уже лежит в localStorage — не пересчитываем, чтобы подписи в
// истории не менялись от версии браузера.
export async function refineDeviceName(nav = globalThis.navigator) {
  if (stored?.name) return rec.name
  const uad = nav?.userAgentData
  if (!uad?.getHighEntropyValues) return rec.name
  try {
    const hints = await uad.getHighEntropyValues(['model', 'platform', 'platformVersion', 'fullVersionList'])
    const name = deviceName({ ua: nav.userAgent || '', hints: { platform: uad.platform, ...hints }, id: rec.id })
    if (name && name !== rec.name) { rec = { ...rec, name }; store(rec) }
  } catch (e) {
    logErr('device: Client Hints недоступны, имя по User-Agent', e)
  }
  return rec.name
}
refineDeviceName()
