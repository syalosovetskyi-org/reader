"""Клавиша F / «а» — полный экран и обратно (Серж 28.11.2026). Прод не трогается.
Run with Playwright installed: python tests/browser/fullscreen_key.py

1. F при фокусе на странице включает полный экран, второе F — выключает.
2. «а» (ru/uk раскладка, та же физическая клавиша KeyF) при фокусе внутри
   книги (iframe главы) — так же туда и обратно.
3. Ctrl+F не перехватывается; в поле ввода F — просто буква.
"""
import asyncio
import io
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
CHAPTERS = 2
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
            body = ''.join(f'<p>Chapter {i}, paragraph {j:03d}. ' + 'Plain English text for many pages. ' * 6 + '</p>' for j in range(300))
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




async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    data = epub()
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=['--no-sandbox'])
        ctx = await browser.new_context(viewport={'width': 1200, 'height': 900}, service_workers='block')
        async def api(route):
            path = route.request.url.split('/api/', 1)[1]
            if path == 'reader/1/file':
                await route.fulfill(body=data, content_type='application/epub+zip', headers={'X-Book-Format': 'epub'}); return
            payload = {}
            if path == 'library/1': payload = WORK
            elif path == 'library': payload = [WORK]
            elif path == 'progress/1': payload = {'ratio': 0, 'locator': '', 'text_anchor': ''}
            elif path.endswith('/history') or path in ('monitored', 'calibre/books', 'bookmarks/1', 'highlights/1', 'progress/finished'): payload = []
            elif path == 'ingest/jobs': payload = {'active': 0, 'jobs': []}
            await route.fulfill(json=payload)
        await ctx.route('**/api/**', api)
        page = await ctx.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.goto(url + '/?open=1')
        await page.wait_for_function("async () => (await import('/js/core/state.js')).view?.lastLocation?.cfi")
        await page.wait_for_timeout(500)
        fs = "() => !!document.fullscreenElement"

        async def wait_fs(on):
            await page.wait_for_function(fs if on else "() => !document.fullscreenElement", timeout=3000)

        # 1. фокус на странице, английская F
        await page.evaluate("document.activeElement?.blur?.()")
        await page.keyboard.press('f')
        await wait_fs(True)
        await page.keyboard.press('f')
        await wait_fs(False)
        print('1 ok: F на странице', flush=True)

        # 2. фокус в книге, «а» на клавише KeyF (как в ru/uk раскладке)
        # Фокус в документ главы (iframe): клавиши тогда приходят туда, а не в страницу.
        await page.evaluate("""async () => { const d = (await import('/js/core/state.js')).view.renderer.getContents()[0].doc
          d.defaultView.focus(); d.body.tabIndex = -1; d.body.focus() }""")
        assert await page.evaluate("async () => (await import('/js/core/state.js')).view.renderer.getContents()[0].doc.hasFocus()"), 'фокус не в книге'
        await page.keyboard.press('f')
        await wait_fs(True)
        await page.keyboard.press('f')
        await wait_fs(False)
        print('2 ok: F при фокусе в книге', flush=True)

        # 2b. логика «а»: вызывает переключение (шпион вместо настоящего API)
        n = await page.evaluate("""async () => {
          let calls = 0
          const el = document.documentElement
          el.requestFullscreen = () => { calls++; return Promise.resolve() }
          const d = (await import('/js/core/state.js')).view.renderer.getContents()[0].doc
          d.dispatchEvent(new KeyboardEvent('keydown', {key: 'а', code: 'KeyF', bubbles: true}))
          document.dispatchEvent(new KeyboardEvent('keydown', {key: 'А', code: 'KeyF', shiftKey: true, bubbles: true}))
          document.dispatchEvent(new KeyboardEvent('keydown', {key: 'f', code: 'KeyF', ctrlKey: true, bubbles: true}))
          document.dispatchEvent(new KeyboardEvent('keydown', {key: 'а', code: 'KeyF', repeat: true, bubbles: true}))
          const inp = document.createElement('input'); document.body.append(inp)
          inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'f', code: 'KeyF', bubbles: true})); inp.remove()
          return calls }""")
        assert n == 2, f'ожидалось 2 переключения (а в книге, А на странице), было {n}'
        print('3 ok: «а»/«А» переключают, Ctrl+F, автоповтор и поле ввода — нет', flush=True)
        assert not errors, errors
        await browser.close()
    server.shutdown()
    print('fullscreen_key: ok')


asyncio.run(main())
