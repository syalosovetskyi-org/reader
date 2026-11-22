// Сопоставление мест между изданиями (serg/tasks#3240): главы и поиск фрагмента.
// Запуск: node tests/js/test_edition_map.mjs
import assert from 'node:assert/strict'
import { chapterNumber, buildChapters, matchChapter, mapSection, findWords, anchorWords } from '../../frontend/js/core/edition-map.js'

assert.equal(chapterNumber('Глава 5. Встреча'), 5)
assert.equal(chapterNumber('Chapter XII'), 12)
assert.equal(chapterNumber('7. Night'), 7)
assert.equal(chapterNumber('Пролог'), null)

// RU: 2 титульные секции, потом главы 1..8; EN: сразу главы.
const ru = buildChapters([{ label: 'Титул', index: 0 }, { label: 'Содержание', index: 1 },
  ...Array.from({ length: 8 }, (_, i) => ({ label: `Глава ${i + 1}`, index: i + 2 }))], 10)
const en = buildChapters(Array.from({ length: 8 }, (_, i) => ({ label: `Chapter ${i + 1}`, index: i })), 8)
assert.equal(matchChapter(en[5], en, ru).label, 'Глава 6', 'по номеру в заголовке')
const ruNoNum = buildChapters([{ label: 'Титул', index: 0 }, { label: 'Одна', index: 1 }, { label: 'Две', index: 2 }], 3)
const enNoNum = buildChapters([{ label: 'One', index: 0 }, { label: 'Two', index: 1 }], 2)
// Без номеров сдвиг не вычислить — берётся тот же порядковый номер.
assert.equal(matchChapter(enNoNum[1], enNoNum, ruNoNum).label, 'Одна')

// Секция 5 EN, середина → секция 7 RU («Глава 6»), та же доля.
const m = mapSection({ chapters: en, sections: 8 }, { chapters: ru, sections: 10 }, 5, 0.5)
assert.equal(m.index, 7)
assert.equal(m.chapter.label, 'Глава 6')
assert.ok(Math.abs(m.fraction - 0.5) < 1e-9)

// Подпункты одной секции схлопываются; глава на две секции делится по доле.
const nested = buildChapters([{ label: 'Глава 1', index: 0 }, { label: '1.1', index: 0 }, { label: 'Глава 2', index: 2 }], 4)
assert.equal(nested.length, 2)
const m2 = mapSection({ chapters: en, sections: 8 }, { chapters: nested, sections: 4 }, 0, 0.75)
assert.equal(m2.index, 1)
assert.ok(Math.abs(m2.fraction - 0.5) < 1e-9)

// Поиск фрагмента: точно, затем нечётко (PDF: перенос строки, ё, кавычки).
const hay = anchorWords('Начало. Он дочитал страницу до конца и только потом заметил, что в комнате стало тихо. Конец.')
const exact = findWords(hay, anchorWords('Он дочитал страницу до конца и только потом'))
assert.deepEqual([exact.start, exact.exact], [1, true])
const fuzzy = findWords(hay, anchorWords('«Он дочи- тал страницу до конца, и только потом заметил, что в комнате'))
assert.ok(fuzzy && !fuzzy.exact && fuzzy.start <= 2 && fuzzy.score >= 0.7, JSON.stringify(fuzzy))
assert.equal(findWords(hay, anchorWords('совершенно другой абзац из другой книги вовсе')), null)

console.log('ok: edition-map — главы, доля, поиск фрагмента')
