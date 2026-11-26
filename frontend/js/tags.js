import { api } from './core/api.js'
import { escapeHtml, toast } from './core/dom.js'
import { logErr } from './core/log.js'
import { GENRE_GROUPS } from './core/tag-filter.js'

let dictionary = []
let request = null
export function cachedTags() { return dictionary }
export function getTags() {
  if (!request) request = api.get('/api/tags').then(tags => {
    if (!Array.isArray(tags)) throw new Error('Некорректный словарь тегов')
    dictionary = tags
    return tags
  }).catch(err => { request = null; throw err })
  return request
}
export function invalidateTags() { request = null }
export function tagError(context, err) {
  logErr(context, err)
  toast(`${context}: ${err?.data?.detail || err?.message || 'ошибка сети'}`, 'err')
}
export const STATUS_LABELS = { done: 'Дописана', wip: 'Пишется', dead: 'Заброшена?' }
export const SOURCE_LABELS = { meta: 'Метаданные', desc: 'Описание', site: 'Сайт', search: 'Поиск', read: 'Чтение книги', manual: 'Вручную' }
export function statusBadge(book) {
  const label = STATUS_LABELS[book.write_status]
  return label ? `<span class="tag-status st-${book.write_status}" title="${escapeHtml(SOURCE_LABELS[book.write_status_source] || '')}"><span aria-hidden="true">●</span> ${label}</span>` : ''
}
export function tagClass(tag) {
  return GENRE_GROUPS.has(tag.group) ? ' c-genre' : tag.group === 'Контент' ? ' c-warn' : ''
}

// Удерживаем Tab в мобильном листе/модалке; Escape обрабатывает владелец.
export function trapTagFocus(event, root) {
  if (event.key !== 'Tab') return
  const items = [...root.querySelectorAll('button:not(:disabled), input, select, [tabindex="0"]')]
    .filter(el => !el.closest('[hidden]') && el.getClientRects().length)
  if (!items.length) { event.preventDefault(); root.focus(); return }
  const first = items[0], last = items.at(-1)
  if (event.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) {
    event.preventDefault(); last.focus()
  } else if (!event.shiftKey && (document.activeElement === last || !root.contains(document.activeElement))) {
    event.preventDefault(); first.focus()
  }
}
