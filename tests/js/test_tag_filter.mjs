import assert from 'node:assert/strict'
import { filterBooks, countWithTag, primaryTags, groupTags, normalizeTag } from '../../frontend/js/core/tag-filter.js'

const tags = [
  { id: 1, name: 'Мироустройство', group: 'Темы и тропы' },
  { id: 2, name: 'Фэнтези', group: 'Жанр' },
  { id: 3, name: 'ЛитРПГ', group: 'Жанр' },
  { id: 4, name: 'Python', group: 'IT: языки и технологии' },
  { id: 5, name: 'Насилие', group: 'Контент' },
  { id: 6, name: 'История', group: 'Нон-фикшн' },
  { id: 7, name: 'Архитектура', group: 'IT: темы' },
]
const books = [
  { id: 1, title: 'Ёлка', author: 'Автор', write_status: 'done', tag_ids: [1, 2, 3] },
  { id: 2, title: 'Вторая', write_status: 'wip', tag_ids: [2, 5] },
  { id: 3, title: 'Третья', series: 'Ёлки', write_status: 'done', tag_ids: [2] },
  { id: 4, title: 'Четвёртая', write_status: 'dead', tag_ids: [5] },
  { id: 5, title: 'Пустая' },
]
const ids = filter => filterBooks(books, filter).map(book => book.id)
assert.deepEqual(ids({}), [1, 2, 3, 4, 5])
assert.deepEqual(ids({ tagIds: [2, 3] }), [1], 'И, а не ИЛИ')
assert.deepEqual(ids({ status: 'done', tagIds: [2] }), [1, 3])
assert.deepEqual(ids({ status: 'wip', tagIds: [2], query: 'ВТОР' }), [2])
assert.deepEqual(ids({ status: 'dead', tagIds: [2] }), [])
assert.deepEqual(ids({ tagIds: [99] }), [])
assert.deepEqual(ids({ query: 'елк', tagIds: [2], status: 'done' }), [1, 3])
assert.equal(normalizeTag('  ЕЩЁ '), 'еще')

const selected = { status: 'done', tagIds: [2] }
assert.equal(countWithTag(books, selected, 3), 1)
assert.equal(countWithTag(books, selected, 2), 2, 'выбранный тег не снимается при подсчёте')
assert.equal(countWithTag(books, selected, 5), 0)
assert.equal(countWithTag(books, { ...selected, query: 'елка' }, 3), 1)
assert.deepEqual(selected, { status: 'done', tagIds: [2] }, 'подсчёт не меняет фильтр')

assert.deepEqual(primaryTags(books[0], tags).map(tag => tag.id), [2, 3])
assert.deepEqual(primaryTags({ tag_ids: [5, 4, 1] }, tags).map(tag => tag.id), [4, 1])
assert.deepEqual(primaryTags({ tag_ids: [7, 6, 5] }, tags).map(tag => tag.id), [6, 7])
assert.deepEqual(primaryTags({ tag_ids: [999] }, tags), [])
assert.deepEqual(primaryTags({}, tags), [])
assert.deepEqual(primaryTags({ tag_ids: [5] }, tags).map(tag => tag.id), [5])
assert.deepEqual(groupTags([tags[4], tags[2], tags[0]], tags).map(([group]) => group), ['Темы и тропы', 'Жанр', 'Контент'])
console.log('tag_filter: ok (И, статус, поиск, счётчики, главные теги, порядок групп)')
