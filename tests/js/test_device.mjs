// Имя устройства (serg/tasks#3239): таблица кодов моделей и сборка имени по
// подставным userAgentData и UA. Запуск: node tests/js/test_device.mjs
import assert from 'node:assert/strict'

// В Node 22 navigator только для чтения — подменяем через defineProperty.
const uadHints = { model: 'SM-S918B', platformVersion: '14.0.0' }
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: {
    userAgent: 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36',
    userAgentData: { platform: 'Android', getHighEntropyValues: async () => uadHints },
  },
})
const store = new Map()
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) },
})

const d = await import('../../frontend/js/core/device.js')

assert.equal(d.modelName('SM-S918B'), 'Galaxy S23 Ultra')
assert.equal(d.modelName('SM-F936U1'), 'Galaxy Z Fold4')
assert.equal(d.modelName('M2102J20SG'), 'POCO X3')
assert.equal(d.modelName('M2007J20CG'), 'POCO X3')
assert.equal(d.modelName('SM-A546B'), 'SM-A546B', 'неизвестный код — как есть')

const WIN = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
assert.equal(d.deviceName({ ua: WIN, hints: { platform: 'Windows', platformVersion: '15.0.0' }, id: 'ab12cd34-0000' }), 'Windows 11 · Chrome · ab12')
assert.equal(d.deviceName({ ua: WIN, hints: { platform: 'Windows', platformVersion: '10.0.0' }, id: 'ab12' }), 'Windows 10 · Chrome · ab12')
assert.equal(d.deviceName({ ua: WIN + ' Edg/131.0', id: 'ff00' }), 'Windows · Edge · ff00')
assert.equal(d.deviceName({ ua: navigator.userAgent, hints: { platform: 'Android', model: 'SM-S918B' }, id: 'x' }), 'Galaxy S23 Ultra · Chrome')
assert.equal(d.deviceName({ ua: 'Mozilla/5.0 (Linux; Android 11; M2102J20SG Build/RKQ1) AppleWebKit Chrome/120 Mobile Safari', id: 'x' }), 'POCO X3 · Chrome')
assert.equal(d.deviceName({ ua: navigator.userAgent, id: 'cd34' }), 'Android · Chrome · cd34', 'урезанный UA без модели')
assert.equal(d.deviceName({ ua: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit Version/17.0 Mobile Safari/604.1', id: 'x' }), 'iPhone · Safari')
assert.equal(d.deviceName({ ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Gecko/20100101 Firefox/130.0', id: 'beef' }), 'Mac · Firefox · beef')
assert.equal(d.deviceName({ ua: 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120 YaBrowser/24.1 Safari/537.36', id: 'beef' }), 'Linux · Яндекс · beef')

// Модуль: id и имя считаются один раз и кешируются в localStorage; Client
// Hints уточняют имя до торгового.
const name = await d.refineDeviceName()
assert.equal(name, 'Galaxy S23 Ultra · Chrome')
const saved = JSON.parse(store.get('reader.device.v1'))
assert.equal(saved.id, d.deviceId())
assert.equal(saved.name, 'Galaxy S23 Ultra · Chrome')
assert.equal(d.identity().device, 'Galaxy S23 Ultra · Chrome')
assert.ok(d.sessionId && d.sessionId !== d.deviceId())
console.log('ok: имя устройства по Client Hints и UA, кеш в localStorage')
