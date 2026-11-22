"""Поиск по мере ввода в живом Chromium + foliate. Прод не трогается.
Run with Playwright installed: python tests/browser/live_search.py

1. Результаты появляются при наборе, без Enter; одна буква не ищет.
2. Регистр, «ё» = «е», слово с мягким переносом и через границу тегов находится.
3. Клик по результату открывает нужную главу и ставит переход в историю.
4. Новый запрос отменяет старый: в списке только результаты последнего.
5. Совпадения подсвечены на экране (оверлей foliate).
"""
import asyncio
import io
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
CHAPTERS = 6
WORK = dict(id=1, title='Поиск', author='Тест', file_format='epub',
            chapters_count=CHAPTERS, content_updated_at='2026-11-22T00:00:00')


def epub():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''')
        z.writestr('book.opf', '''<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">test-search</dc:identifier><dc:title>Поиск</dc:title><dc:language>ru</dc:language></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>''' + ''.join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(CHAPTERS)) + '''</manifest><spine>''' + ''.join(f'<itemref idref="c{i}"/>' for i in range(CHAPTERS)) + '</spine></package>')
        z.writestr('nav.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Главы</title></head><body><nav epub:type="toc"><ol>' + ''.join(f'<li><a href="c{i}.xhtml">Глава {i}</a></li>' for i in range(CHAPTERS)) + '</ol></nav></body></html>')
        for i in range(CHAPTERS):
            body = ''.join(f'<p>Глава {i}, абзац {j:02d}. ' + 'Обычный текст для нескольких страниц. ' * 8 + '</p>' for j in range(12))
            if i == 4:
                body += '<p>Тут Ёлка и <i>Каб</i>ба­ла, а ещё Метатрон.</p>'
            if i == 1:
                body += '<p>Тут елка без точек и КАББАЛА заглавными.</p>'
            z.writestr(f'c{i}.xhtml', f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Глава {i}</title></head><body>{body}</body></html>')
    return buf.getvalue()


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'frontend'), **kwargs)

    def log_message(self, *args):
        pass


SECTION = "async () => (await import('/js/core/state.js')).view.lastLocation?.section?.current"
RESULTS = "() => [...document.querySelectorAll('#search-results .search-result')].map(a => a.textContent)"
META = "() => document.querySelector('#search-meta').textContent"


async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    data = epub()
    state = {'history': []}
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=['--no-sandbox'])
        ctx = await browser.new_context(viewport={'width': 1200, 'height': 900}, service_workers='block')
        await ctx.add_init_script("localStorage.setItem('reader.prefs', JSON.stringify({v:3, theme:'day', fontFamily:'system', fontScale:1, marginLevel:2, columns:1, flow:'paginated'}))")

        async def api(route):
            req = route.request
            path = req.url.split('/api/', 1)[1]
            if path == 'reader/1/file':
                await route.fulfill(body=data, content_type='application/epub+zip', headers={'X-Book-Format': 'epub'})
                return
            if path == 'progress/1/history' and req.method == 'POST':
                state['history'].append(req.post_data_json)
                await route.fulfill(json={'ok': True})
                return
            payload = {}
            if path == 'library/1': payload = WORK
            elif path == 'library': payload = [WORK]
            elif path == 'progress/1': payload = {'ratio': 0, 'locator': '', 'text_anchor': ''}
            elif path == 'progress': payload = {}
            elif path == 'progress/finished': payload = []
            elif path.endswith('/history') or path in ('monitored', 'calibre/books', 'bookmarks/1', 'highlights/1'): payload = []
            elif path == 'ingest/jobs': payload = {'active': 0, 'jobs': []}
            elif path == 'reader/1/toc': payload = {'items': []}
            await route.fulfill(json=payload)

        await ctx.route('**/api/**', api)
        page = await ctx.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.goto(url + '/?open=1')
        await page.wait_for_function("async () => (await import('/js/core/state.js')).view?.lastLocation?.cfi")
        await page.wait_for_timeout(800)

        # --- 1. одна буква не ищет, две — ищут без Enter ---
        await page.click('#search-btn')
        await page.type('#search-input', 'к', delay=30)
        await page.wait_for_timeout(500)
        assert await page.evaluate(RESULTS) == [], 'одна буква не ищет'
        assert 'ещё букву' in await page.evaluate(META)
        await page.type('#search-input', 'аббал', delay=40)
        await page.wait_for_function("() => document.querySelector('#search-meta').textContent.startsWith('Найдено')", timeout=10000)
        res = await page.evaluate(RESULTS)
        # --- 2. регистр и перенос/теги ---
        assert len(res) == 2, res
        assert res[0].startswith('Глава 1') and 'КАББАЛА' in res[0], res
        assert res[1].startswith('Глава 4') and 'Кабба­ла' in res[1], res
        print('1-2 ok: live, регистр, перенос, теги', flush=True)

        # ё = е
        await page.fill('#search-input', 'елка')
        await page.dispatch_event('#search-input', 'input')
        await page.wait_for_function("() => document.querySelector('#search-meta').textContent.startsWith('Найдено: 2')", timeout=10000)
        print('ё=е ok', flush=True)

        # --- 4. новый запрос отменяет старый ---
        await page.fill('#search-input', 'абзац')
        await page.dispatch_event('#search-input', 'input')
        await page.wait_for_timeout(60)
        await page.fill('#search-input', 'метатрон')
        await page.dispatch_event('#search-input', 'input')
        await page.wait_for_function("() => document.querySelector('#search-meta').textContent.startsWith('Найдено')", timeout=10000)
        await page.wait_for_timeout(400)
        res = await page.evaluate(RESULTS)
        assert len(res) == 1 and 'Метатрон' in res[0], res
        print('4 ok: отмена старого запроса', flush=True)

        # --- 3. клик → глава 4, в истории переход 'search' ---
        await page.evaluate("() => window.dispatchEvent(new PointerEvent('pointerdown'))")
        await page.click('#search-results .search-result')
        await page.wait_for_function("async () => (await import('/js/core/state.js')).view.lastLocation?.section?.current === 4", timeout=5000)
        await page.wait_for_timeout(600)
        assert any(r.get('reason') == 'search' for r in state['history']), state['history']
        print('3 ok: переход и история', flush=True)

        # --- 5. подсветка на экране ---
        await page.wait_for_function("""async () => {
          const v = (await import('/js/core/state.js')).view
          const c = v.renderer.getContents().find(x => x.index === 4)
          return !!c?.overlayer?.element?.querySelector('rect,path,g *')
        }""", timeout=5000)
        print('5 ok: подсветка', flush=True)

        # Пусто — очищает список (панель закрылась после перехода — открываем снова).
        await page.click('#search-btn')
        await page.fill('#search-input', '')
        await page.dispatch_event('#search-input', 'input')
        await page.wait_for_timeout(400)
        assert await page.evaluate(RESULTS) == []

        await browser.close()
    server.shutdown()
    assert not errors, errors
    print('live_search: ok')


asyncio.run(main())
