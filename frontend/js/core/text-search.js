// Быстрый поиск по тексту секции (поиск по мере ввода).
//
// Почему не view.search() из foliate: он на каждый запрос заново разбирает
// каждую секцию (а в PDF заново строит текстовый слой каждой страницы) и
// сравнивает строку Intl.Collator-ом на каждой позиции — на большой главе это
// ~1,2 с против ~0,04 с у indexOf. Здесь текст секции нормализуется один раз
// и кэшируется, а дальше каждый запрос — это indexOf по готовой строке.
//
// Нормализация сохраняет обратную карту позиций, чтобы найденное место
// превратилось в Range исходного документа (и в CFI):
//  * регистр не важен, «ё» = «е» (как в базовом поиске foliate);
//  * мягкие переносы и невидимые символы форматирования выбрасываются —
//    иначе слово с переносом внутри не находится;
//  * любая цепочка пробелов, переводов строк и неразрывных пробелов — один пробел.


function lowerChar(c) {
  const l = c.toLowerCase()
  // Длину символа менять нельзя (карта позиций посимвольная): «İ» → «i̇» не трогаем.
  if (l.length !== c.length) return c
  return l === 'ё' ? 'е' : l
}

// strs — тексты узлов в порядке документа (как отдаёт textWalker foliate).
// Возвращает { text, map, starts }: text — нормализованная строка,
// map[i] — позиция i-го символа text в склейке strs, starts — начала узлов.
export function normalizeStrs(strs) {
  const starts = new Array(strs.length)
  let total = 0
  for (let i = 0; i < strs.length; i++) { starts[i] = total; total += strs[i].length }
  const raw = strs.join('')
  // Нижний регистр целиком — быстро; если длина поменялась (редкие буквы
  // вроде «İ»), падаем на посимвольный вариант, где длина сохраняется.
  let low = raw.toLowerCase()
  if (low.length !== raw.length) low = Array.from(raw, lowerChar).join('')
  const codes = new Uint16Array(total)
  const map = new Int32Array(total + 1)
  let n = 0
  let prevSpace = true // ведущие пробелы не нужны
  for (let pos = 0; pos < total; pos++) {
    let c = low.charCodeAt(pos)
    if (isInvisible(c)) continue
    if (isSpace(c)) {
      if (prevSpace) continue
      codes[n] = 32; map[n++] = pos; prevSpace = true
      continue
    }
    if (c === 0x451) c = 0x435 // ё → е
    codes[n] = c; map[n++] = pos; prevSpace = false
  }
  map[n] = total // конец строки — для конца последнего совпадения
  let text = ''
  for (let i = 0; i < n; i += 8192) text += String.fromCharCode.apply(null, codes.subarray(i, Math.min(n, i + 8192)))
  return { text, map: map.subarray(0, n + 1), starts, total }
}

const isInvisible = (c) => c === 0xAD || (c >= 0x200B && c <= 0x200D) || c === 0x2060 || c === 0xFEFF
const isSpace = (c) => c === 32 || (c >= 9 && c <= 13) || c === 0xA0 || c === 0x1680
  || (c >= 0x2000 && c <= 0x200A) || c === 0x2028 || c === 0x2029 || c === 0x202F
  || c === 0x205F || c === 0x3000

export function normalizeQuery(q) {
  let s = ''
  let prevSpace = true
  for (const c of String(q || '')) {
    if (c.length === 1 && isInvisible(c.charCodeAt(0))) continue
    if (c.length === 1 && isSpace(c.charCodeAt(0))) { if (!prevSpace) s += ' '; prevSpace = true; continue }
    s += c.length === 1 ? lowerChar(c) : c.toLowerCase()
    prevSpace = false
  }
  return s.trimEnd()
}

// Позиция в склейке strs → [номер узла, смещение в узле].
export function locate(starts, pos) {
  let lo = 0
  let hi = starts.length - 1
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (starts[mid] <= pos) lo = mid
    else hi = mid - 1
  }
  return [lo, pos - starts[lo]]
}

const CONTEXT = 50
const squash = (s) => s.replace(/\s+/g, ' ')

// Все совпадения needle (уже нормализованного) в секции.
// Возвращает [{ range: {startIndex,startOffset,endIndex,endOffset}, excerpt }].
export function findInSection(sec, raw, needle, limit = Infinity) {
  const res = []
  if (!needle) return res
  const { text, map, starts } = sec
  let from = 0
  while (res.length < limit) {
    const i = text.indexOf(needle, from)
    if (i < 0) break
    from = i + 1
    const a = map[i]
    // Конец — сразу за последним символом совпадения в исходной строке.
    const b = map[i + needle.length - 1] + 1
    const [startIndex, startOffset] = locate(starts, a)
    // Конец ровно на границе узлов относим к предыдущему узлу: Range не
    // должен заходить в следующий узел нулевой длиной.
    const [endIndex, endOffset] = locate(starts, b - 1)
    const pre = squash(raw.slice(Math.max(0, a - CONTEXT * 2), a)).trimStart()
    const post = squash(raw.slice(b, b + CONTEXT * 2)).trimEnd()
    res.push({
      range: { startIndex, startOffset, endIndex, endOffset: endOffset + 1 },
      excerpt: {
        pre: (pre.length > CONTEXT ? '…' : '') + pre.slice(-CONTEXT),
        match: squash(raw.slice(a, b)),
        post: post.slice(0, CONTEXT) + (post.length > CONTEXT ? '…' : ''),
      },
    })
  }
  return res
}
