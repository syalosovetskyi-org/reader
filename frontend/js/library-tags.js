import { $, escapeHtml } from './core/dom.js'
import { logErr } from './core/log.js'
import { countWithTag, filterBooks, groupTags, normalizeTag } from './core/tag-filter.js'
import { cachedTags, getTags, tagError, trapTagFocus } from './tags.js'

const KEY = 'reader.tagFilter.v1'
export const tagFilter = { status: '', tagIds: [] }
try {
  const saved = JSON.parse(localStorage.getItem(KEY) || 'null')
  if (saved) {
    tagFilter.status = ['', 'done', 'wip', 'dead'].includes(saved.status) ? saved.status : ''
    tagFilter.tagIds = Array.isArray(saved.tagIds) ? [...new Set(saved.tagIds.filter(Number.isInteger))] : []
  }
} catch (err) { logErr('Не удалось восстановить фильтр тегов', err) }

let onChange = () => {}, getBooks = () => []
let draft = null, opened = false, mobile = false
const phone = window.matchMedia('(max-width: 600px)')
const statuses = [['', 'Все'], ['done', 'Дописаны'], ['wip', 'Пишутся'], ['dead', 'Заброшены']]
const statusButtons = state => statuses.map(([value, label]) =>
  `<button type="button" data-tag-status="${value}" aria-pressed="${state.status === value}">${label}</button>`).join('')

function commit() {
  try { localStorage.setItem(KEY, JSON.stringify(tagFilter)) }
  catch (err) { tagError('Не удалось сохранить фильтр', err) }
  onChange()
}
export function selectLibraryTag(id) {
  tagFilter.status = ''
  tagFilter.tagIds = [Number(id)]
  commit()
}
function toggleTag(state, id) {
  state.tagIds = state.tagIds.includes(id) ? state.tagIds.filter(x => x !== id) : [...state.tagIds, id]
}
function closePicker(focus = true) {
  if (!opened) return
  opened = false
  draft = null
  $('#lib-tag-picker').hidden = true
  $('#lib-tag-scrim').hidden = true
  document.body.classList.remove('tag-sheet-open')
  $('#lib-tags-btn').setAttribute('aria-expanded', 'false')
  $('#lib-filter-mobile').setAttribute('aria-expanded', 'false')
  if (focus) $(mobile ? '#lib-filter-mobile' : '#lib-tags-btn').focus()
}
function openPicker() {
  mobile = phone.matches
  draft = mobile ? { status: tagFilter.status, tagIds: [...tagFilter.tagIds] } : null
  opened = true
  const picker = $('#lib-tag-picker')
  picker.classList.toggle('is-sheet', mobile)
  picker.setAttribute('role', mobile ? 'dialog' : 'region')
  if (mobile) picker.setAttribute('aria-modal', 'true')
  else picker.removeAttribute('aria-modal')
  picker.hidden = false
  $('#lib-tag-scrim').hidden = !mobile
  document.body.classList.toggle('tag-sheet-open', mobile)
  $('#lib-tags-btn').setAttribute('aria-expanded', String(!mobile))
  $('#lib-filter-mobile').setAttribute('aria-expanded', String(mobile))
  $('#lib-tag-q').value = ''
  renderPicker()
  $('#lib-tag-q').focus()
}
function renderPicker() {
  if (!opened) return
  const state = draft || tagFilter
  $('#lib-sheet-status').hidden = !mobile
  $('#lib-sheet-status').innerHTML = `<h3>Статус</h3><div class="tag-segment" role="group" aria-label="Статус">${statusButtons(state)}</div>`
  $('#lib-sheet-footer').hidden = !mobile
  const books = getBooks()
  const query = normalizeTag($('#lib-tag-q').value)
  const tags = cachedTags().filter(tag => normalizeTag(tag.name).includes(query))
  $('#lib-tag-groups').innerHTML = groupTags(tags, cachedTags()).map(([group, items]) => {
    const chips = items.map(tag => {
      const chosen = state.tagIds.includes(tag.id)
      const n = countWithTag(books, state, tag.id)
      return n || chosen ? `<button type="button" class="tag-chip${chosen ? ' on' : ''}" data-tag-id="${tag.id}" aria-pressed="${chosen}">${escapeHtml(tag.name)} <span class="tag-n">${n}</span></button>` : ''
    }).join('')
    return chips ? `<section><h3>${escapeHtml(group)}</h3><div class="tag-chips">${chips}</div></section>` : ''
  }).join('') || '<p class="tag-muted">Такого тега нет</p>'
  $('#lib-tag-apply').textContent = `Показать ${filterBooks(books, state).length}`
}
export function renderLibraryFilters(baseBooks, filtered) {
  $('#lib-status-segment').innerHTML = statusButtons(tagFilter)
  $('#lib-picked-tags').innerHTML = tagFilter.tagIds.map(id => {
    const tag = cachedTags().find(t => t.id === id)
    return `<button type="button" class="tag-chip on" data-picked-tag="${id}" aria-label="Убрать тег ${escapeHtml(tag?.name || String(id))}">${escapeHtml(tag?.name || String(id))} <span aria-hidden="true">×</span></button>`
  }).join('')
  $('#lib-tag-count').textContent = `${filtered.length} из ${baseBooks.length} книг`
  const active = tagFilter.tagIds.length + Number(Boolean(tagFilter.status))
  $('#lib-filter-mobile').textContent = `Фильтр · ${active}`
  renderPicker()
}
export function mountLibraryFilters(options) {
  onChange = options.onChange
  getBooks = options.getBooks
  $('#lib-status-segment').addEventListener('click', event => {
    const button = event.target.closest('[data-tag-status]')
    if (!button) return
    tagFilter.status = button.dataset.tagStatus
    commit()
    $('#lib-status-segment').querySelector(`[data-tag-status="${tagFilter.status}"]`)?.focus()
  })
  $('#lib-picked-tags').addEventListener('click', event => {
    const button = event.target.closest('[data-picked-tag]')
    if (button) { toggleTag(tagFilter, Number(button.dataset.pickedTag)); commit(); $('#lib-tags-btn').focus() }
  })
  for (const id of ['#lib-tags-btn', '#lib-filter-mobile']) $(id).addEventListener('click', () => opened ? closePicker() : openPicker())
  $('#lib-tag-picker').addEventListener('click', event => {
    const tag = event.target.closest('[data-tag-id]')
    const status = event.target.closest('[data-tag-status]')
    if (tag) toggleTag(draft || tagFilter, Number(tag.dataset.tagId))
    else if (status) (draft || tagFilter).status = status.dataset.tagStatus
    else return
    if (draft) renderPicker()
    else commit()
    // Восстановить фокус после обновления списка, в том числе при нулевом счётчике.
    const selector = tag ? `[data-tag-id="${tag.dataset.tagId}"]` : `[data-tag-status="${status.dataset.tagStatus}"]`
    $('#lib-tag-picker').querySelector(selector)?.focus()
  })
  $('#lib-tag-q').addEventListener('input', renderPicker)
  $('#lib-tag-reset').addEventListener('click', () => { draft = { status: '', tagIds: [] }; renderPicker() })
  $('#lib-tag-apply').addEventListener('click', () => {
    Object.assign(tagFilter, draft)
    closePicker()
    commit()
  })
  $('#lib-tag-close').addEventListener('click', () => closePicker())
  $('#lib-tag-scrim').addEventListener('click', () => closePicker())
  document.addEventListener('click', event => {
    if (opened && !mobile && !event.target.closest('#lib-tag-picker, #lib-tags-btn')) closePicker(false)
  })
  document.addEventListener('keydown', event => {
    if (!opened) return
    if (event.key === 'Escape') { event.preventDefault(); closePicker() }
    else if (mobile) trapTagFocus(event, $('#lib-tag-picker'))
  })
  phone.addEventListener('change', () => closePicker(false))
  // Открытие книги/переход назад не оставляют лист поверх другого экрана.
  window.addEventListener('popstate', () => closePicker(false))
}

export async function loadLibraryTags() {
  try { await getTags() }
  catch (err) { tagError('Не удалось загрузить теги', err) }
}
