// Чистая логика: словарь сохраняет серверный порядок групп и тегов.
export const GENRE_GROUPS = new Set(['Жанр', 'Нон-фикшн', 'IT: темы', 'IT: языки и технологии'])
export const normalizeTag = value => String(value || '').toLowerCase().replace(/ё/g, 'е').trim()

export function matchesTagFilter(book, { status = '', tagIds = [], query = '' } = {}) {
  const q = normalizeTag(query)
  return (!status || book.write_status === status)
    && tagIds.every(id => (book.tag_ids || []).includes(id))
    && (!q || [book.title, book.author, book.series].some(value => normalizeTag(value).includes(q)))
}

export function filterBooks(books, filter = {}) {
  return books.filter(book => matchesTagFilter(book, filter))
}

export function countWithTag(books, filter, tagId) {
  return filterBooks(books, { ...filter, tagIds: [...new Set([...(filter.tagIds || []), tagId])] }).length
}

export function primaryTags(book, dictionary) {
  const ids = new Set(book.tag_ids || [])
  const tags = dictionary.filter(tag => ids.has(tag.id))
  return [...tags.filter(tag => GENRE_GROUPS.has(tag.group)),
    ...tags.filter(tag => !GENRE_GROUPS.has(tag.group))].slice(0, 2)
}

export function groupTags(tags, dictionary = tags) {
  const groups = new Map(dictionary.map(tag => [tag.group, []]))
  for (const tag of tags) {
    if (!groups.has(tag.group)) groups.set(tag.group, [])
    groups.get(tag.group).push(tag)
  }
  return [...groups].filter(([, items]) => items.length)
}
