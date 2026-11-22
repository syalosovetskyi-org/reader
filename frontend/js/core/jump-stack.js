// Автомат «переход / Назад» (serg/tasks#3239). Без DOM и сети: всё внешнее —
// зависимостями, поэтому его проверяет node (tests/js/test_jump_stack.mjs).
//
// Правила:
//  * переход — только явное действие (оглавление, сноска, поиск, закладка,
//    шкала, глава ±1 не с последней страницы, Ctrl+Home/End, панель переходов,
//    открытие на главе). Вызывающий оборачивает его в navigate(reason, go):
//    исход запоминается ДО go, а в стек и на сервер он попадает, только если
//    страница действительно сменилась;
//  * «Назад» — чистый pop локального стека, на сервер он не пишет ничего:
//    иначе «Назад» клал текущее место наверх, и следующее «Назад» возвращало
//    обратно (пинг-понг двух позиций);
//  * пустой стек один раз засевается серверными строками, дальше «Назад»
//    идёт вглубь;
//  * пришли переходом на место с вершины стека (обратная ссылка сноски) —
//    вершина снимается, а не добавляется новая запись.

export const MAX_LOCAL = 50

export function isEmptyPos(p) {
  return !p || (!p.locator && !p.text_anchor && !p.ratio)
}

// Одно место: при обоих CFI — по CFI (якорь бывает неуникален: одинаковая
// авторская плашка в конце каждой главы), иначе по якорю, иначе по доле.
export function samePos(a, b) {
  if (!a || !b) return false
  if (a.locator && b.locator) return a.locator === b.locator
  if (a.text_anchor && b.text_anchor) {
    return a.text_anchor.startsWith(b.text_anchor) || b.text_anchor.startsWith(a.text_anchor)
  }
  if (!a.locator && !b.locator && !a.text_anchor && !b.text_anchor) {
    return Math.abs((a.ratio || 0) - (b.ratio || 0)) < 1e-9
  }
  return false
}

/**
 * deps:
 *  current()          → снимок текущего места {locator, text_anchor, ratio, chapter, book_rev, device, device_id}
 *  settle()           → Promise: дождаться relocate после перехода
 *  goTo(pos)          → Promise<boolean>: вернуться на сохранённое место (Назад)
 *  post(row)          → Promise: записать снимок в серверную историю
 *  loadRows()         → Promise<rows>: серверная история, новые первыми
 *  newId()            → строка для client_id
 *  onChange()         → перерисовать кнопку
 */
export function createJumpStack(deps) {
  let stack = []
  let seeded = false
  let busy = null

  const top = () => stack[stack.length - 1]
  const changed = () => deps.onChange?.()

  function push(pos, reason, { post = true } = {}) {
    if (isEmptyPos(pos)) return null
    if (top() && samePos(top(), pos)) return null
    const row = { ...pos, reason, created_at: new Date().toISOString(), local: true, client_id: deps.newId() }
    stack.push(row)
    if (stack.length > MAX_LOCAL) stack.shift()
    changed()
    if (post) {
      const { local, created_at, ...body } = row
      // Сеть упала — локальный «Назад» всё равно работает; ошибка — в лог.
      Promise.resolve().then(() => deps.post(body))
        .catch((e) => deps.onError?.('не сохранил переход в историю', e))
    }
    return row
  }

  // Явный переход. Возвращает 'pushed' | 'popped' | 'same' | 'failed'.
  async function navigate(reason, go, { post = true } = {}) {
    const from = deps.current()
    let ok = true
    try {
      ok = (await go()) !== false
    } catch (e) {
      ok = false
      deps.onError?.('переход не удался', e)
    }
    await deps.settle()
    const to = deps.current()
    if (!ok && samePos(from, to)) return 'failed'
    if (isEmptyPos(from) || samePos(from, to)) return 'same'
    // Обратная ссылка (сноска → текст): пришли туда, откуда уходили.
    if (top() && samePos(top(), to)) {
      stack.pop()
      changed()
      return 'popped'
    }
    push(from, reason, { post })
    return 'pushed'
  }

  // Положить место без перехода (открытие книги на главе: исход — серверная
  // позиция, которую на экране не показывали).
  function remember(pos, reason, opts) {
    if (samePos(pos, deps.current())) return null
    return push(pos, reason, opts)
  }

  async function seed() {
    seeded = true
    let rows = []
    try {
      rows = (await deps.loadRows()) || []
    } catch (e) {
      deps.onError?.('история переходов не загрузилась', e)
      return false
    }
    // Старые строки «back» писало само «Назад» — именно они давали пинг-понг.
    const usable = rows.filter((r) => r && r.reason !== 'back' && !isEmptyPos(r))
    const seen = []
    for (const r of usable.slice().reverse()) {
      if (seen.length && samePos(seen[seen.length - 1], r)) continue
      seen.push({ ...r, local: false })
    }
    stack = seen.slice(-MAX_LOCAL)
    changed()
    return true
  }

  // «Назад»: pop, пропуская записи, совпадающие с текущим местом.
  async function back() {
    if (busy) return busy
    busy = (async () => {
      if (!stack.length && !seeded) await seed()
      const cur = deps.current()
      let target = stack.pop()
      while (target && samePos(target, cur)) target = stack.pop()
      changed()
      if (!target) return null
      await deps.goTo(target)
      return target
    })()
    try { return await busy } finally { busy = null }
  }

  return {
    navigate,
    back,
    remember,
    reset() { stack = []; seeded = false; changed() },
    get size() { return stack.length },
    get seeded() { return seeded },
    entries() { return stack.slice() },
  }
}
