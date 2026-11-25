"""Перевод видимого текста в живом Chromium + foliate. Прод не трогается.
Run with Playwright installed: python tests/browser/translate_visible.py

foliate растягивает iframe главы на всю главу, и «видимое» по окну iframe было
всей главой: в /api/translate уходило 300 абзацев, сервер отвечал 413.
1. В обоих режимах («Страницы», «Лента») запрос — один экран, не глава,
   и в пределах потолков сервера (80 абзацев, 40 000 знаков).
2. Первый запрос — начало главы; абзац на экране подменён переводом.
3. Листание вперёд переводит то, что доехало до экрана.
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



async def run(flow):
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    data = epub(); sent = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=['--no-sandbox'])
        ctx = await browser.new_context(viewport={'width': 1200, 'height': 900}, service_workers='block')
        await ctx.add_init_script("localStorage.setItem('reader.prefs', JSON.stringify({v:3, theme:'day', fontFamily:'system', fontScale:1, marginLevel:2, columns:1, flow:'%s'}))" % flow)
        async def api(route):
            req = route.request
            path = req.url.split('/api/', 1)[1]
            if path == 'reader/1/file':
                await route.fulfill(body=data, content_type='application/epub+zip', headers={'X-Book-Format': 'epub'}); return
            if path == 'translate':
                items = req.post_data_json['items']; sent.append([i['text'] for i in items])
                await route.fulfill(json={'items': [{'id': i['id'], 'text': 'RU '+i['text'], 'changed': True} for i in items], 'translated': len(items)}); return
            payload = {}
            if path == 'library/1': payload = WORK
            elif path == 'library': payload = [WORK]
            elif path == 'progress/1': payload = {'ratio': 0, 'locator': '', 'text_anchor': ''}
            elif path.endswith('/history') or path in ('monitored', 'calibre/books', 'bookmarks/1', 'highlights/1','progress/finished'): payload = []
            elif path == 'ingest/jobs': payload = {'active': 0, 'jobs': []}
            await route.fulfill(json=payload)
        await ctx.route('**/api/**', api)
        page = await ctx.new_page()
        await page.goto(url + '/?open=1')
        await page.wait_for_function("async () => (await import('/js/core/state.js')).view?.lastLocation?.cfi")
        await page.wait_for_timeout(800)
        await page.evaluate("async () => (await import('/js/translate.js')).toggleTranslate()")
        await page.wait_for_function("() => true")
        for _ in range(50):
            if len(sent) >= 2: break
            await page.wait_for_timeout(100)
        assert sent, 'перевод не запрошен'
        first = sent[0]
        assert 0 < len(first) <= 40, (flow, len(first))
        assert sum(len(t) for t in first) <= 40000
        assert any('paragraph 000.' in t for t in first), first[:2]
        assert not any('paragraph 299.' in t for t in first)
        txt = await page.evaluate("""async () => { const d = (await import('/js/core/state.js')).bookDoc
          return [...d.querySelectorAll('p')].slice(0, 2).map(p => p.textContent.slice(0, 20)) }""")
        assert all(t.startswith('RU ') for t in txt), txt
        print(f'{flow}: 1-2 ok, экран {len(first)} абзацев', flush=True)
        seen = {t for b in sent for t in b}
        n0 = len(sent)
        for _ in range(3):
            await page.evaluate("async () => (await import('/js/core/state.js')).view.next()")
            await page.wait_for_timeout(700)
        new_texts = [t for b in sent[n0:] for t in b if t not in seen]
        assert new_texts, (flow, 'листание не перевело новое')
        assert all(len(b) <= 40 for b in sent)
        print(f'{flow}: 3 ok, при листании +{len(new_texts)} абзацев', flush=True)
        await browser.close()
    server.shutdown()

async def main():
    for f in ('paginated', 'scrolled'):
        await run(f)
    print('translate_visible: ok')


asyncio.run(main())
