import { api } from './core/api.js'
import { escapeHtml } from './core/dom.js'
import { groupTags, normalizeTag } from './core/tag-filter.js'
import { cachedTags, getTags, SOURCE_LABELS, STATUS_LABELS, statusBadge, tagClass, tagError } from './tags.js'

// Монтируется заново при смене книги. Ответы старой страницы не меняют новую.
export function mountBookTags(root, statusRoot, book, { onTag, onSaved }) {
  let editing = false, adding = false, busy = false
  const alive = () => root.isConnected && statusRoot.isConnected
  const label = value => escapeHtml(String(value || ''))
  function render() {
    if (!alive()) return
    const tags = book.tags || []
    const loaded = Array.isArray(book.tags)
    root.innerHTML = `<div class="book-tags-head"><h2>Теги</h2><button type="button" class="btn-ghost" data-edit ${!loaded || busy ? 'disabled' : ''}>${editing ? 'Готово' : 'Изменить'}</button></div>`
      + (tags.length ? groupTags(tags, cachedTags()).map(([group, items]) =>
        `<div class="tag-row"><span class="tag-group-label">${label(group)}</span><div class="tag-chips">${items.map(tag =>
          `<button type="button" class="tag-chip${tagClass(tag)}" data-book-tag="${tag.id}" title="${label(SOURCE_LABELS[tag.source])}" ${busy ? 'disabled' : ''} ${editing ? `aria-label="Удалить тег ${label(tag.name)}"` : ''}>${label(tag.name)}${editing ? ' <span aria-hidden="true">×</span>' : ''}</button>`
        ).join('')}</div></div>`).join('') : `<p class="tag-muted">${loaded ? 'Теги ещё не проставлены' : 'Загружаю теги…'}</p>`)
      + (editing ? `<button type="button" class="tag-chip tag-add" data-add ${busy ? 'disabled' : ''}>+ тег</button><div class="book-tag-add" ${adding ? '' : 'hidden'}><input type="search" data-tag-query aria-label="Найти тег" placeholder="Найти тег…" autocomplete="off"><div class="tag-chips" data-suggestions></div></div>` : '')
    statusRoot.innerHTML = editing
      ? `<label class="tag-status-edit">Статус <select aria-label="Статус книги" ${busy ? 'disabled' : ''}><option value="">Неизвестен</option>${Object.entries(STATUS_LABELS).map(([value, text]) => `<option value="${value}" ${book.write_status === value ? 'selected' : ''}>${text}</option>`).join('')}</select></label>`
      : statusBadge(book)
    if (adding) suggestions()
  }
  function suggestions() {
    const query = normalizeTag(root.querySelector('[data-tag-query]').value)
    const have = new Set((book.tags || []).map(tag => tag.id))
    root.querySelector('[data-suggestions]').innerHTML = groupTags(cachedTags().filter(tag =>
      !have.has(tag.id) && normalizeTag(tag.name).includes(query)), cachedTags()).map(([group, items]) =>
      `<section><h3>${label(group)}</h3><div class="tag-chips">${items.map(tag => `<button type="button" class="tag-chip${tagClass(tag)}" data-choose-tag="${tag.id}" ${busy ? 'disabled' : ''}>${label(tag.name)}</button>`).join('')}</div></section>`
    ).join('') || '<p class="tag-muted">Такого тега нет</p>'
  }
  async function changeTags(id, remove) {
    if (busy) return
    busy = true
    render()
    try {
      const tags = remove ? await api.delete(`/api/library/${book.id}/tags/${id}`)
        : await api.post(`/api/library/${book.id}/tags`, { tag_id: id })
      book.tags = tags
      book.tag_ids = tags.map(tag => tag.id)
      onSaved(book)
      adding = false
    } catch (err) { tagError(remove ? 'Не удалось удалить тег' : 'Не удалось добавить тег', err) }
    finally {
      busy = false
      render()
      if (alive()) root.querySelector('[data-add]')?.focus()
    }
  }
  root.addEventListener('click', async event => {
    if (busy) return
    if (event.target.closest('[data-edit]')) {
      editing = !editing; adding = false; render(); root.querySelector('[data-edit]').focus()
    } else if (event.target.closest('[data-add]')) {
      adding = !adding
      render()
      if (adding) {
        root.querySelector('[data-tag-query]').focus()
        try { await getTags(); if (alive() && adding) suggestions() }
        catch (err) { tagError('Не удалось загрузить теги', err) }
      }
    } else {
      const tag = event.target.closest('[data-book-tag]')
      const choice = event.target.closest('[data-choose-tag]')
      if (tag) {
        if (editing) await changeTags(Number(tag.dataset.bookTag), true)
        else onTag(Number(tag.dataset.bookTag))
      } else if (choice) await changeTags(Number(choice.dataset.chooseTag), false)
    }
  })
  root.addEventListener('input', event => { if (event.target.matches('[data-tag-query]')) suggestions() })
  statusRoot.addEventListener('change', async event => {
    if (busy || !event.target.matches('select')) return
    const status = event.target.value
    busy = true; render()
    try {
      Object.assign(book, await api.put(`/api/library/${book.id}/write-status`, { status }))
      onSaved(book)
    } catch (err) { tagError('Не удалось изменить статус', err) }
    finally { busy = false; render(); if (alive()) statusRoot.querySelector('select')?.focus() }
  })
  render()
  getTags().then(() => { if (alive() && !editing) render() })
    .catch(err => { if (alive()) tagError('Не удалось загрузить теги', err) })
}
