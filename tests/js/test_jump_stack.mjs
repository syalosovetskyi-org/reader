// Автомат «переход / Назад» (serg/tasks#3239): настоящий core/jump-stack.js,
// книга — список мест, сеть — счётчик POST.
// Запуск: node tests/js/test_jump_stack.mjs
import assert from 'node:assert/strict'
import { createJumpStack, samePos } from '../../frontend/js/core/jump-stack.js'

function harness(rows = []) {
  const place = (name) => ({ locator: `cfi-${name}`, text_anchor: `text ${name}`, ratio: 0.1, chapter: name })
  const book = { at: place('A') }
  const posts = []
  const gone = []
  let n = 0
  const jumps = createJumpStack({
    current: () => ({ ...book.at }),
    settle: async () => {},
    goTo: async (pos) => { gone.push(pos.chapter); book.at = { ...pos }; return true },
    post: async (row) => { posts.push(row) },
    loadRows: async () => rows,
    newId: () => `id-${++n}`,
    onError: (what, e) => { throw new Error(`${what}: ${e}`) },
  })
  const go = (name) => async () => { book.at = place(name) }
  return { jumps, book, posts, gone, place, go }
}

// 1. A → B → C (оглавление), три «Назад»: C, B, A — и ни одного POST от «Назад».
{
  const h = harness()
  assert.equal(await h.jumps.navigate('toc', h.go('B')), 'pushed')
  assert.equal(await h.jumps.navigate('toc', h.go('C')), 'pushed')
  assert.equal(await h.jumps.navigate('toc', h.go('D')), 'pushed')
  assert.equal(h.posts.length, 3)
  for (const want of ['C', 'B', 'A']) {
    const t = await h.jumps.back()
    assert.equal(t.chapter, want)
    assert.equal(h.book.at.chapter, want)
  }
  assert.equal(h.posts.length, 3, '«Назад» не пишет на сервер')
  assert.equal(await h.jumps.back(), null, 'дальше некуда — засев с пустого сервера ничего не дал')
  assert.deepEqual(h.posts.map((p) => p.client_id), ['id-1', 'id-2', 'id-3'])
  assert.ok(h.posts.every((p) => p.reason === 'toc' && !('local' in p)))
  console.log('ok: A→B→C→D, три «Назад» — C, B, A без единого POST')
}

// 2. Тот же экран после перехода — не запись.
{
  const h = harness()
  assert.equal(await h.jumps.navigate('toc', async () => {}), 'same')
  assert.equal(h.jumps.size, 0)
  assert.equal(h.posts.length, 0)
  console.log('ok: переход без смены страницы ничего не пишет')
}

// 3. Засев с сервера проходится вглубь без цикла.
{
  const p = (name, reason = 'toc') => ({ locator: `cfi-${name}`, text_anchor: `text ${name}`, ratio: 0.2, chapter: name, reason })
  // Новые первыми; среди них старая строка «back» — её засев пропускает.
  const rows = [p('A', 'tab'), p('X', 'back'), p('B'), p('C'), p('C', 'overwrite'), p('D')]
  const h = harness(rows)
  const seen = []
  for (let i = 0; i < 10; i++) {
    const t = await h.jumps.back()
    if (!t) break
    seen.push(t.chapter)
  }
  // Стоим в A: верхняя строка (A) пропускается, дальше вглубь B, C, D — и конец.
  assert.deepEqual(seen, ['B', 'C', 'D'])
  assert.equal(h.posts.length, 0)
  console.log('ok: засев серверной историей идёт вглубь без пинг-понга', seen.join(' → '))
}

// 4. Сноска: туда — запись, обратная ссылка на место с вершины — вершина снимается.
{
  const h = harness()
  assert.equal(await h.jumps.navigate('link', h.go('FN')), 'pushed')
  assert.equal(h.jumps.size, 1)
  assert.equal(await h.jumps.navigate('link', h.go('A')), 'popped')
  assert.equal(h.jumps.size, 0)
  assert.equal(h.posts.length, 1)
  console.log('ok: обратная ссылка из сноски снимает вершину стека')
}

// 5. Переход без серверной отправки (вкладка догнала другое устройство).
{
  const h = harness()
  assert.equal(await h.jumps.navigate('device', h.go('Z'), { post: false }), 'pushed')
  assert.equal(h.posts.length, 0)
  assert.equal((await h.jumps.back()).chapter, 'A')
  console.log('ok: переход на свежее место кладёт своё в локальный стек без POST')
}

// 6. Сравнение мест: при обоих CFI решает CFI, а не одинаковая плашка-якорь.
assert.equal(samePos({ locator: 'a', text_anchor: 'плашка' }, { locator: 'b', text_anchor: 'плашка' }), false)
assert.equal(samePos({ locator: '', text_anchor: 'двенадцать слов' }, { locator: '', text_anchor: 'двенадцать слов и ещё двенадцать' }), true)
assert.equal(samePos({ ratio: 0.3 }, { ratio: 0.3 }), true)
console.log('ok: samePos')
