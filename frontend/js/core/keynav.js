// Какая навигация стоит за нажатой клавишей (serg/tasks#996).
//
// Отдельный модуль БЕЗ импортов — намеренно: navigation.js тянет десяток
// зависимостей (dom, api, prefs, state, tts, library…) и в node не грузится,
// а решение «куда ведёт клавиша» должно проверяться тестом, а не глазами.
// Тем же приёмом живёт core/fullscreen.js.
//
// Поручение владельца: End — в конец ГЛАВЫ, Ctrl+End — в конец КНИГИ; то же с
// Home. Раньше Home/End уводили сразу к краям КНИГИ, и добраться до конца
// длинной главы одной клавишей было нечем.

const PLAIN = {
  ArrowLeft: 'prev',
  ArrowRight: 'next',
  PageUp: 'prev',
  PageDown: 'next',
}

const STEP = new Set([' ', 'Spacebar', 'Enter'])

// Полный экран — клавиша F (Серж 28.11.2026: «по нажатию ф английской или а
// русско-украинской»). В русской и украинской раскладках на месте F стоит «а»,
// поэтому смотрим прежде всего на физическую клавишу (event.code = 'KeyF'), а
// по event.key — на случай экранной клавиатуры, где code бывает пустым.
const FULLSCREEN_KEYS = new Set(['f', 'F', 'а', 'А'])

/**
 * @param {string} key — event.key
 * @param {{ctrl?: boolean, shift?: boolean, alt?: boolean, code?: string}} mods
 *   code — event.code (физическая клавиша, от раскладки не зависит)
 * @returns {'prev'|'next'|'chapterStart'|'chapterEnd'|'bookStart'|'bookEnd'|'fullscreen'|null}
 *   null — клавиша не наша, событие достаётся браузеру.
 */
export function keyNavAction(key, { ctrl = false, shift = false, alt = false, code = '' } = {}) {
  // Ctrl/Alt+F — поиск браузера и меню: их не трогаем. Shift разрешён (Caps Lock).
  if (!ctrl && !alt && (code === 'KeyF' || FULLSCREEN_KEYS.has(key))) return 'fullscreen'
  if (key === 'Home') return ctrl ? 'bookStart' : 'chapterStart'
  if (key === 'End') return ctrl ? 'bookEnd' : 'chapterEnd'
  // Ctrl модифицирует ТОЛЬКО Home/End. Ctrl+PageDown у браузера — переключение
  // вкладок, Ctrl+стрелка — переход по словам: молчаливый угон чужого сочетания
  // человек заметит не сразу и списать его будет не на что.
  if (ctrl) return null
  if (STEP.has(key)) return shift ? 'prev' : 'next'
  return PLAIN[key] ?? null
}
