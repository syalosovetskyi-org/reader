"""Масштаб PDF в живом Chromium + foliate (serg/tasks#3274). Прод не трогается.
Run with Playwright installed: python tests/browser/pdf_zoom.py

1. Лента на широком окне: по умолчанию «По ширине» (как раньше), панель
   масштаба видна только у PDF.
2. «100%» — страница 600 pt = 800 CSS px, по центру; «+» — 110%.
3. 300% — страница шире окна: горизонтальная прокрутка, страница по центру.
4. Место чтения не сдвигается при смене масштаба.
5. «Страница целиком» — страница влезает по высоте.
6. Ctrl + колесо внутри страницы — масштаб страницы, а не окна браузера.
7. Ctrl 0 — назад к умолчанию; масштаб помнится для книги после переоткрытия.
8. Режим «Страницы»: 150% — страница шире, не обрезана слева.
"""
import asyncio
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
WORK = dict(id=1, title='Масштаб', author='Тест', file_format='pdf',
            chapters_count=1, content_updated_at='2026-11-24T00:00:00')


def pdf(count=12):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               ('<< /Type /Pages /Kids [' + ' '.join(f'{4+i*2} 0 R' for i in range(count)) + f'] /Count {count} >>').encode(),
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    for i in range(count):
        stream = f'BT /F1 24 Tf 50 700 Td (Page {i+1}) Tj ET'.encode()
        objects += [f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Contents {5+i*2} 0 R /Resources << /Font << /F1 3 0 R >> >> >>'.encode(),
                    b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream']
    data, offsets = b'%PDF-1.4\n', [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(data)
    data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{n:010} 00000 n \n'.encode() for n in offsets[1:])
    return data + f'trailer\n<< /Root 1 0 R /Size {len(offsets)} >>\nstartxref\n{start}\n%%EOF'.encode()


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'frontend'), **kwargs)

    def log_message(self, *args):
        pass


PREFS = "localStorage.setItem('reader.prefs', JSON.stringify({v:3, theme:'day', fontFamily:'system', fontScale:1, marginLevel:2, columns:1, flow:'%s', pdfCrop:false})); localStorage.setItem('reader:pdf-as-epub', '0')"
# Ширина страницы и прокрутка ленты — изнутри теневого DOM рендерера.
GEOM = """async () => {
  const v = (await import('/js/core/state.js')).view, r = v.renderer
  const f = (r.getContents().find(c => c.index === r.index) || r.getContents()[0]).doc.defaultView.frameElement
  const box = f.getBoundingClientRect(), host = r.getBoundingClientRect()
  return { w: box.width, h: box.height, left: box.left - host.left, view: r.clientWidth, viewH: r.clientHeight,
           scrollW: r.scrollWidth, scrollLeft: r.scrollLeft, scale: r.currentScale,
           index: r.index, label: document.querySelector('#pdf-zoom-val').textContent,
           shown: !document.querySelector('#pdf-zoom').hidden, page: v.lastLocation?.section?.current,
           frac: v.lastLocation?.fraction }
}"""
REOPEN = """async work => { const m = await import('/js/reader-core.js'); await m.openReader(work); await new Promise(r => setTimeout(r, 800)) }"""


READY = "async () => { const v = (await import('/js/core/state.js')).view; return !!(v?.isFixedLayout && v.renderer?.getContents?.().some(c => c.doc?.defaultView?.frameElement)) }"


async def wait_ready(page, timeout=30):
    # wait_for_function с async-предикатом не ждёт промис — опрашиваем сами.
    for _ in range(timeout * 5):
        if await page.evaluate(READY):
            return
        await page.wait_for_timeout(200)
    raise AssertionError('PDF не открылся')


async def geom(page):
    await wait_ready(page)
    return await page.evaluate(GEOM)


async def settle(page, ms=500):
    await page.wait_for_timeout(ms)


async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    data = pdf()
    errors = []
    report = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=['--no-sandbox'])

        async def run(flow, phone=False):
            ctx = await browser.new_context(viewport={'width': 412, 'height': 800} if phone else {'width': 1600, 'height': 900},
                                            service_workers='block', is_mobile=phone, has_touch=phone)
            await ctx.add_init_script(PREFS % flow)

            async def api(route):
                req = route.request
                path = req.url.split('/api/', 1)[1].split('?')[0]
                if path == 'reader/1/file':
                    hdr = {'X-Book-Format': 'pdf', 'Accept-Ranges': 'bytes', 'ETag': '"z1"', 'X-Book-SHA1': 'z1',
                           'Content-Type': 'application/pdf'}
                    rng = req.headers.get('range')
                    if req.method == 'HEAD':
                        await route.fulfill(status=200, headers={**hdr, 'Content-Length': str(len(data))}, body=b'')
                        return
                    if rng:
                        a, b = re.match(r'bytes=(\d+)-(\d*)', rng).groups()
                        a = int(a); b = min(len(data) - 1, int(b) if b else len(data) - 1)
                        await route.fulfill(status=206, headers={**hdr, 'Content-Range': f'bytes {a}-{b}/{len(data)}'}, body=data[a:b + 1])
                        return
                    await route.fulfill(status=200, headers=hdr, body=data)
                    return
                payload = {}
                if path == 'library/1': payload = WORK
                elif path == 'library': payload = [WORK]
                elif path == 'progress/1': payload = {'ratio': 0, 'locator': '', 'text_anchor': ''}
                elif path == 'progress': payload = {}
                elif path == 'progress/finished': payload = []
                elif path == 'reader/1/convert': payload = {'status': '', 'ready': False, 'convertible': False}
                elif path.endswith('/history') or path in ('monitored', 'calibre/books', 'bookmarks/1', 'highlights/1'): payload = []
                elif path == 'ingest/jobs': payload = {'active': 0, 'jobs': []}
                elif path == 'reader/1/toc': payload = {'items': []}
                await route.fulfill(json=payload)

            await ctx.route('**/api/**', api)
            page = await ctx.new_page()
            page.on('pageerror', lambda e: errors.append(f'{flow}: {e}'))
            await page.goto(url + '/?open=1')
            await wait_ready(page)
            # Панель масштаба включается по reader-opened — после восстановления места.
            await page.wait_for_function("!document.querySelector('#pdf-zoom').hidden", timeout=20000)
            await settle(page, 1200)
            return ctx, page

        # ---------------- Лента ----------------
        ctx, page = await run('scrolled')
        g = await geom(page)
        assert g['shown'] and g['label'] == 'По ширине', g
        assert abs(g['w'] - g['view']) < 2, g
        report['1_default_fit_width'] = True; print('1_default_fit_width', flush=True)

        # уходим на середину 4-й страницы, чтобы проверять, что место держится
        await page.evaluate("async () => { const v = (await import('/js/core/state.js')).view; await v.renderer.goTo({ index: 3, anchor: 0.5 }) }")
        await settle(page)
        before = await geom(page)

        await page.click('#pdf-zoom-val')
        await page.click('#pdf-zoom-menu [data-z="1"]')
        await settle(page)
        g = await geom(page)
        assert abs(g['w'] - 800) < 2 and g['label'] == '100%', g
        assert abs(g['left'] - (g['view'] - 800) / 2) < 3, ('по центру', g)
        assert g['page'] == 3 and abs(g['frac'] - before['frac']) < 0.02, ('место держится', before, g)
        await page.click('#pdf-zoom-in')
        await settle(page)
        g = await geom(page)
        assert g['label'] == '110%' and abs(g['w'] - 880) < 2, g
        report['2_100_and_plus'] = True; print('2_100_and_plus', flush=True)
        report['4_position_kept'] = True; print('4_position_kept', flush=True)

        await page.evaluate("async () => { const m = await import('/js/pdf-zoom.js'); m.setZoom(3) }")
        await settle(page)
        g = await geom(page)
        assert abs(g['w'] - 2400) < 3 and g['scrollW'] >= 2400, g
        assert abs(g['scrollLeft'] - (2400 - g['view']) / 2) < 3, ('прокрутка по центру', g)
        report['3_wider_than_window'] = True; print('3_wider_than_window', flush=True)

        await page.click('#pdf-zoom-val')
        await page.click('#pdf-zoom-menu [data-z="fit-page"]')
        await settle(page)
        g = await geom(page)
        assert g['label'] == 'Страница' and abs(g['h'] - g['viewH']) < 3, g
        report['5_fit_page'] = True; print('5_fit_page', flush=True)

        # Ctrl + колесо внутри iframe страницы
        await page.click('#pdf-zoom-val'); await page.click('#pdf-zoom-menu [data-z="1"]'); await settle(page)
        zoom_before = await page.evaluate('window.visualViewport.scale')
        box = await page.evaluate("async () => { const r = (await import('/js/core/state.js')).view.renderer; const f = (r.getContents().find(c => c.index === r.index) || r.getContents()[0]).doc.defaultView.frameElement.getBoundingClientRect(); return [f.left + f.width / 2, Math.max(f.top, 0) + 200] }")
        await page.mouse.move(*box)
        await page.keyboard.down('Control')
        for _ in range(3):
            await page.mouse.wheel(0, -100)
            await page.wait_for_timeout(30)
        await page.keyboard.up('Control')
        await settle(page, 600)
        g = await geom(page)
        assert g['label'] not in ('100%', 'По ширине') and int(g['label'].rstrip('%')) > 100, g
        assert await page.evaluate('window.visualViewport.scale') == zoom_before
        report['6_ctrl_wheel'] = g['label']; print('6_ctrl_wheel', flush=True)

        # Ctrl+= c клавиатуры, потом Ctrl 0
        await page.keyboard.press('Control+0')
        await settle(page)
        g = await geom(page)
        assert g['label'] == 'По ширине', g

        # Запоминается для книги
        await page.evaluate("async () => { const m = await import('/js/pdf-zoom.js'); m.setZoom(1.25) }")
        await settle(page)
        await page.evaluate(REOPEN, WORK)
        await page.wait_for_function("document.querySelector('#pdf-zoom-val').textContent === '125%'", timeout=15000)
        await settle(page, 800)
        g = await geom(page)
        assert abs(g['w'] - 1000) < 3, g
        report['7_reset_and_remembered'] = True; print('7_reset_and_remembered', flush=True)
        await ctx.close()

        # ---------------- Страницы ----------------
        ctx, page = await run('paginated')
        g = await geom(page)
        assert g['shown'] and g['label'] == 'Страница', g
        await page.evaluate("async () => { const m = await import('/js/pdf-zoom.js'); m.setZoom(1.5) }")
        await settle(page, 800)
        g = await geom(page)
        assert abs(g['w'] - 1200) < 3, g
        assert g['h'] > g['viewH'] and g['left'] >= -1, ('не обрезана сверху/слева', g)
        report['8_paginated_zoom'] = True; print('8_paginated_zoom', flush=True)
        await ctx.close()

        # ---------------- Телефон: щипок ----------------
        ctx, page = await run('scrolled', phone=True)
        g0 = await geom(page)
        assert not await page.is_visible('#pdf-zoom-val'), 'на телефоне по умолчанию панели масштаба нет'
        cdp = await ctx.new_cdp_session(page)
        cx, cy = 206, 400

        async def touch(kind, pts):
            await cdp.send('Input.dispatchTouchEvent', {'type': kind, 'touchPoints': [{'x': x, 'y': y, 'id': i} for i, (x, y) in enumerate(pts)]})
        await touch('touchStart', [(cx - 40, cy), (cx + 40, cy)])
        for k in range(1, 9):
            await touch('touchMove', [(cx - 40 - k * 10, cy), (cx + 40 + k * 10, cy)])
            await page.wait_for_timeout(16)
        await touch('touchEnd', [])
        await settle(page, 800)
        g = await geom(page)
        assert g['label'].endswith('%') and g['scale'] > g0['scale'] * 1.4, (g0, g)
        assert await page.is_visible('#pdf-zoom-val'), 'свой масштаб на телефоне виден числом'
        assert g['page'] == 0, ('щипок не листает', g)
        assert await page.evaluate('window.visualViewport.scale') == 1, 'браузер не масштабировал страницу целиком'
        report['9_phone_pinch'] = g['label']; print('9_phone_pinch', flush=True)
        await ctx.close()

        await browser.close()
    server.shutdown()
    bad = [e for e in errors if 'render' not in e.lower()]
    assert not bad, bad
    print(report)
    print('pdf_zoom: ok')


asyncio.run(main())
