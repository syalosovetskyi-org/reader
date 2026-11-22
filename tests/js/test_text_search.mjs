// Ядро поиска по мере ввода. Запуск: node tests/js/test_text_search.mjs
import assert from 'node:assert/strict'
import { normalizeStrs, normalizeQuery, findInSection } from '../../frontend/js/core/text-search.js'

const find = (strs, q) => {
  const sec = normalizeStrs(strs)
  return findInSection(sec, strs.join(''), normalizeQuery(q))
}
const cut = (strs, r) => {
  // Восстановить исходный фрагмент по range — как сделает DOM Range.
  if (r.startIndex === r.endIndex) return strs[r.startIndex].slice(r.startOffset, r.endOffset)
  return strs[r.startIndex].slice(r.startOffset)
    + strs.slice(r.startIndex + 1, r.endIndex).join('')
    + strs[r.endIndex].slice(0, r.endOffset)
}

// Регистр и ё.
{
  const strs = ['Ещё раз про Каббалу. ', 'еще и КАББАЛА.']
  const r = find(strs, 'ещё')
  assert.equal(r.length, 2)
  assert.equal(cut(strs, r[0].range), 'Ещё')
  assert.equal(cut(strs, r[1].range), 'еще')
  assert.equal(find(strs, 'каббал').length, 2)
}

// Совпадение через границу узлов (<i>Каб</i>бала) и мягкий перенос внутри слова.
{
  const strs = ['про <', 'Каб', 'ба­ла', '> и всё']
  const r = find(strs, 'каббала')
  assert.equal(r.length, 1)
  assert.equal(r[0].range.startIndex, 1)
  assert.equal(r[0].range.endIndex, 2)
  assert.equal(cut(strs, r[0].range), 'Кабба­ла')
  assert.equal(r[0].excerpt.match, 'Кабба­ла')
}

// Пробелы: перевод строки и неразрывный пробел = пробел; в запросе — тоже.
{
  const strs = ['Аарон\n   Смит- Тейлор']
  assert.equal(find(strs, 'аарон смит').length, 1)
  assert.equal(find(strs, 'Аарон   Смит').length, 1)
  assert.equal(find(strs, 'смит- тейлор').length, 1)
}

// Совпадение, кончающееся ровно на конце узла, не залезает в следующий.
{
  const strs = ['Бог', 'и']
  const r = find(strs, 'бог')
  assert.deepEqual(r[0].range, { startIndex: 0, startOffset: 0, endIndex: 0, endOffset: 3 })
}

// Контекст и лимит.
{
  const long = 'а'.repeat(200) + ' слово ' + 'б'.repeat(200)
  const r = find([long], 'слово')
  assert.ok(r[0].excerpt.pre.startsWith('…'))
  assert.ok(r[0].excerpt.post.endsWith('…'))
  const many = find(['ля '.repeat(100)], 'ля')
  assert.equal(many.length, 100)
  const sec = normalizeStrs(['ля '.repeat(100)])
  assert.equal(findInSection(sec, 'ля '.repeat(100), 'ля', 5).length, 5)
}

// Пустой запрос ничего не находит.
assert.equal(find(['текст'], '   ').length, 0)

// Скорость: глава ~250 КБ ищется много быстрее секунды (foliate base — ~1,2 с).
{
  const para = 'Аарон Смит-Тейлор шёл по улице и думал о каббале, о Боге и о том, что Ёлка не ёлка. '
  const strs = Array.from({ length: 3000 }, () => para)
  const t0 = performance.now()
  const sec = normalizeStrs(strs)
  const t1 = performance.now()
  const r = findInSection(sec, strs.join(''), normalizeQuery('каббал'))
  const t2 = performance.now()
  assert.equal(r.length, 3000)
  assert.ok(t2 - t1 < 200, `поиск по готовому тексту ${Math.round(t2 - t1)} мс`)
  console.log(`нормализация ${Math.round(t1 - t0)} мс, поиск ${Math.round(t2 - t1)} мс`)
}
console.log('test_text_search: ok')
