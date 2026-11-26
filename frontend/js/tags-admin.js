import { $, escapeHtml } from './core/dom.js'
import { api } from './core/api.js'
import { logErr } from './core/log.js'
import { normalizeTag } from './core/tag-filter.js'
import { getTags, invalidateTags, tagError, trapTagFocus } from './tags.js'

let overlay = null, timer = null, opener = null, generation = 0
function pendingCount(count) {
  $('#tags-pending-count').textContent = Number.isFinite(count) ? `· ${count}` : ''
}
export async function refreshTagsAdminCount() {
  try { pendingCount((await api.get('/api/tags/progress')).pending_proposals) }
  catch (err) { logErr('Не удалось загрузить число предложений тегов', err) }
}

// Расстояние Левенштейна: предвыбираем ближайшее имя, но решение принимает человек.
function distance(a, b) {
  a = normalizeTag(a); b = normalizeTag(b)
  let row = Array.from({ length: b.length + 1 }, (_, i) => i)
  for (let i = 0; i < a.length; i++) {
    const next = [i + 1]
    for (let j = 0; j < b.length; j++) next.push(Math.min(next[j] + 1, row[j + 1] + 1, row[j] + Number(a[i] !== b[j])))
    row = next
  }
  return row[b.length]
}
function closestTag(proposal, tags) {
  return tags.reduce((best, tag) => !best || distance(proposal.name, tag.name) < distance(proposal.name, best.name) ? tag : best, null)
}
function close() {
  generation++
  clearTimeout(timer)
  overlay.hidden = true
  document.body.classList.remove('tags-admin-open')
  opener?.focus()
}
function renderProgress(progress) {
  const total = Number(progress.total) || 0
  const tagged = Number(progress.tagged) || 0
  const steps = [['meta', 'метаданные'], ['desc', 'описание'], ['search', 'поиск'], ['read', 'чтение']]
  $('#tags-admin-progress').innerHTML = `<span>Разметка библиотеки: размечено ${tagged} из ${total}</span>
    <div class="tags-progress-bar" role="progressbar" aria-label="Разметка библиотеки" aria-valuemin="0" aria-valuemax="${total || 1}" aria-valuenow="${Math.min(total, tagged)}">${steps.map(([key]) =>
      `<i class="tag-step-${key}" style="width:${total ? Math.min(100, Math.max(0, Number(progress.by_step?.[key]) || 0) / total * 100) : 0}%"></i>`).join('')}</div>
    <div class="tags-progress-legend">${steps.map(([key, label]) => `<span class="tag-step-${key}">${label} ${Number(progress.by_step?.[key]) || 0}</span>`).join('')}<span>в очереди ${Number(progress.queued) || 0}</span></div>`
  $('#tags-run').disabled = Boolean(progress.running)
  $('#tags-run').textContent = progress.running ? 'Разметка идёт…' : 'Разметить ещё 40'
  pendingCount(progress.pending_proposals)
  $('#tags-admin-count').textContent = `${progress.pending_proposals || 0} ждут решения`
}
async function pollProgress(token) {
  if (overlay.hidden || token !== generation) return
  try {
    const progress = await api.get('/api/tags/progress')
    if (overlay.hidden || token !== generation) return
    renderProgress(progress)
  } catch (err) {
    logErr('Обновление прогресса тегов', err)
    if (token === generation) $('#tags-admin-count').textContent = 'Не удалось обновить прогресс'
  }
  if (token === generation && !overlay.hidden) timer = setTimeout(() => pollProgress(token), 5000)
}
function renderProposals(proposals, tags) {
  $('#tags-proposals').innerHTML = proposals.length ? proposals.map(proposal => {
    const nearest = closestTag(proposal, tags)
    const books = proposal.books || []
    const extra = Math.max(0, proposal.count - books.length)
    return `<article class="tag-proposal" data-proposal="${proposal.id}">
      <div class="tag-proposal-top"><strong>${escapeHtml(proposal.name)}</strong><span class="tag-muted">предложено в «${escapeHtml(proposal.group)}»</span></div>
      <div class="tag-muted">${proposal.count} книг: ${books.map(book => escapeHtml(book.title)).join(', ')}${extra ? ` и ещё ${extra}` : ''}</div>
      <div class="tag-proposal-actions">
        <button type="button" class="btn-primary" data-action="approve">Добавить</button>
        ${tags.length ? `<label class="tag-merge-label">Слить с <select aria-label="Целевой тег для ${escapeHtml(proposal.name)}">${tags.map(tag => `<option value="${tag.id}" ${tag.id === nearest?.id ? 'selected' : ''}>${escapeHtml(tag.name)} · ${escapeHtml(tag.group)}</option>`).join('')}</select></label><button type="button" class="btn-ghost" data-action="merge">Слить</button>` : ''}
        <button type="button" class="btn-ghost" data-action="reject">Отклонить</button>
      </div></article>`
  }).join('') : '<p class="tag-muted">Новых тегов нет</p>'
}
function mount() {
  if (overlay) return
  overlay = document.createElement('div')
  overlay.id = 'tags-admin-overlay'
  overlay.className = 'modal-overlay'
  overlay.hidden = true
  overlay.innerHTML = `<section class="modal tags-admin" role="dialog" aria-modal="true" aria-labelledby="tags-admin-title" tabindex="-1">
    <header class="tags-admin-head"><h2 id="tags-admin-title">Новые теги</h2><span id="tags-admin-count" class="tag-muted" role="status"></span><button id="tags-admin-close" type="button" class="btn-ghost" aria-label="Закрыть новые теги">✕</button></header>
    <div id="tags-admin-progress" class="tags-progress"></div>
    <button id="tags-run" type="button" class="btn-primary">Разметить ещё 40</button>
    <div id="tags-proposals" aria-live="polite"></div></section>`
  document.body.append(overlay)
  $('#tags-admin-close').addEventListener('click', close)
  overlay.addEventListener('click', event => { if (event.target === overlay) close() })
  overlay.addEventListener('keydown', event => {
    if (event.key !== 'Escape') trapTagFocus(event, overlay)
  })
  // Escape — на документе, а не на окне: после «Разметить ещё» кнопка
  // блокируется, фокус уходит на body, и слушатель окна Escape уже не видел.
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && overlay && !overlay.hidden) { event.preventDefault(); close() }
  })
  $('#tags-run').addEventListener('click', async () => {
    const token = generation
    $('#tags-run').disabled = true
    try {
      const result = await api.post('/api/tags/run?step=2&limit=40', {})
      if (token !== generation) return
      $('#tags-admin-count').textContent = result.started ? `Разметка запущена · в очереди ${result.queued}` : `В очереди ${result.queued}`
      clearTimeout(timer)
      await pollProgress(token)
    } catch (err) {
      tagError('Не удалось запустить разметку', err)
      if (token === generation && !overlay.hidden) $('#tags-run').disabled = false
    }
  })
  $('#tags-proposals').addEventListener('click', async event => {
    const button = event.target.closest('[data-action]')
    if (!button) return
    const card = button.closest('[data-proposal]')
    const controls = [...card.querySelectorAll('button, select')]
    if (card.dataset.busy) return
    card.dataset.busy = 'true'
    controls.forEach(control => { control.disabled = true })
    const action = button.dataset.action
    const name = card.querySelector('strong').textContent
    try {
      await api.post(`/api/tags/proposals/${card.dataset.proposal}/${action}`,
        action === 'merge' ? { into_tag_id: Number(card.querySelector('select').value) } : {})
      card.classList.add('gone')
      const result = { approve: `«${name}» добавлен в словарь`, merge: `«${name}» стал синонимом`, reject: `«${name}» отклонён` }[action]
      card.querySelector('.tag-proposal-actions').innerHTML = `<p class="tag-proposal-result" role="status" tabindex="-1">${escapeHtml(result)}</p>`
      card.querySelector('.tag-proposal-result').focus()
      if (action !== 'reject') invalidateTags()
      window.dispatchEvent(new Event('tags-updated'))
      await refreshTagsAdminCount()
      if (!overlay.hidden) {
        clearTimeout(timer)
        await pollProgress(generation)
      }
    } catch (err) {
      tagError('Не удалось обработать предложение', err)
      delete card.dataset.busy
      controls.forEach(control => { control.disabled = false })
      button.focus()
    }
  })
}
export async function openTagsAdmin() {
  mount()
  opener = document.activeElement
  const token = ++generation
  overlay.hidden = false
  document.body.classList.add('tags-admin-open')
  $('#tags-admin-close').focus()
  $('#tags-proposals').textContent = 'Загружаю предложения…'
  $('#tags-run').disabled = true
  try {
    const [progress, proposals, tags] = await Promise.all([
      api.get('/api/tags/progress'), api.get('/api/tags/proposals'), getTags(),
    ])
    if (token !== generation || overlay.hidden) return
    renderProgress(progress)
    renderProposals(proposals, tags)
    timer = setTimeout(() => pollProgress(token), 5000)
  } catch (err) {
    tagError('Не удалось загрузить очередь тегов', err)
    if (token === generation) $('#tags-proposals').textContent = 'Не удалось загрузить очередь. Закрой и открой «Теги» ещё раз.'
  }
}
$('#tags-admin-btn').addEventListener('click', openTagsAdmin)
