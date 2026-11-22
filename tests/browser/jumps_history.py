"""Переходы и «Назад» в живом Chromium + foliate + НАСТОЯЩИЙ бэкенд (serg/tasks#3239).

Бэкенд поднимается в этом же процессе на временном SQLite (READER_DB_URL
снимается — прод-БД тест не видит), книга загружается через /api/library/upload.
Запуск (нужны зависимости проекта и Playwright):
    python tests/browser/jumps_history.py

1. Сноска pandoc → «Назад» — ровно исходный CFI; в истории одна строка «link».
2. Листание через 3 главы — история пуста.
3. Оглавление — одна запись, «Назад» точен.
4. Две вкладки одного устройства: нетронутая вкладка при возврате переходит
   на свежее место, показывает тост, своё место — в «Назад».
5. Два устройства (разные device_id) по очереди: место другого — строкой
   «device» с ЕГО именем.
6. Офлайн: листание без сети, другое устройство тем временем читает дальше;
   сеть вернулась — досылка не затирает новое место, а ложится строкой offline.
"""
import asyncio
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_tmp = tempfile.mkdtemp(prefix='reader-jumps-')
os.environ.pop('READER_DB_URL', None)
os.environ.update({
    'READER_ALLOW_SQLITE': '1',
    'READER_DATA_DIR': _tmp,
    'READER_BOOKS_DIR': os.path.join(_tmp, 'books'),
    'READER_COVERS_DIR': os.path.join(_tmp, 'covers'),
    'READER_TMP_DIR': os.path.join(_tmp, 'tmp'),
    'READER_TTS_DIR': os.path.join(_tmp, 'tts'),
    'READER_SECRET_KEY_PATH': os.path.join(_tmp, 'secret.key'),
    'READER_DB_PATH': os.path.join(_tmp, 'reader.db'),
})
sys.path.insert(0, str(ROOT))

import uvicorn
from playwright.async_api import async_playwright

from backend.app import ingestjob, scheduler

# Фоновые задания бэкенда тесту не нужны.
scheduler.start = lambda: None
scheduler.shutdown = lambda: None
ingestjob.recover_on_startup = lambda: None
ingestjob.start_workers = lambda: None
ingestjob.stop_workers = lambda mark=False: None

from backend.app.main import app

CHAPTERS = 6


def epub() -> bytes:
    """Главы на несколько страниц; в главе 1 сноска pandoc в начале, текст
    сноски — в <section id="footnotes"> в конце того же файла."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr('book.opf', '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">test-jumps-3239</dc:identifier><dc:title>Сноски и переходы</dc:title><dc:creator>Тест</dc:creator><dc:language>ru</dc:language></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' + ''.join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(CHAPTERS)) + '</manifest><spine>' + ''.join(f'<itemref idref="c{i}"/>' for i in range(CHAPTERS)) + '</spine></package>')
        z.writestr('nav.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Главы</title></head><body><nav epub:type="toc"><ol>' + ''.join(f'<li><a href="c{i}.xhtml">Глава {i}</a></li>' for i in range(CHAPTERS)) + '</ol></nav></body></html>')
        for i in range(CHAPTERS):
            paras = []
            for j in range(14):
                note = '<a href="#fn1" class="footnote-ref" id="fnref1" role="doc-noteref" epub:type="noteref"><sup>1</sup></a>' if (i == 1 and j == 0) else ''
                paras.append(f'<p>Глава {i}, абзац {j:02d}{note}. ' + 'Текст главы для нескольких страниц чтения и переходов. ' * 7 + '</p>')
            if i == 1:
                paras.append('<section id="footnotes" class="footnotes footnotes-end-of-document" role="doc-endnotes" epub:type="footnotes"><hr/><ol><li id="fn1"><p>Текст сноски.<a href="#fnref1" class="footnote-back" role="doc-backlink">↩︎</a></p></li></ol></section>')
            z.writestr(f'c{i}.xhtml', f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Глава {i}</title></head><body><h1>Глава {i}</h1>{"".join(paras)}</body></html>')
    return buf.getvalue()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


STATE = "async () => (await import('/js/core/state.js'))"
CFI = "async () => (await import('/js/core/state.js')).view?.lastLocation?.cfi || ''"
SECTION = "async () => (await import('/js/core/state.js')).view?.lastLocation?.section?.current"
HUMAN = "() => window.dispatchEvent(new PointerEvent('pointerdown'))"


async def open_book(page, url, wid):
    await page.goto(f'{url}/?open={wid}')
    await page.wait_for_function(CFI)
    await page.wait_for_timeout(1800)


async def section_is(page, i):
    return await page.evaluate(SECTION) == i


async def goto_chapter(page, i):
    """Обычное чтение: человек долистал до начала главы i (не переход)."""
    await page.evaluate(HUMAN)
    await page.evaluate(f"async () => {{ await (await import('/js/core/state.js')).view.goTo('c{i}.xhtml') }}")
    assert await until(lambda: section_is(page, i)), ('не доехал до главы', i, await page.evaluate(SECTION))
    await page.wait_for_timeout(1200)


async def history(req, wid):
    r = await req.get(f'/api/progress/{wid}/history')
    assert r.ok, await r.text()
    return await r.json()


async def progress(req, wid):
    return await (await req.get(f'/api/progress/{wid}')).json()


async def click_in_book(page, selector):
    """Настоящий клик мышью по элементу внутри iframe книги."""
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        el = await frame.query_selector(selector)
        if not el:
            continue
        box = await el.bounding_box()
        assert box, ('элемент не на экране', selector)
        await page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        return
    raise AssertionError(f'нет {selector} в книге')


async def back(page):
    await page.click('#jump-back-btn')
    await page.wait_for_timeout(1500)


async def until(check, timeout=12.0, step=0.25):
    """Машина тестов бывает сильно нагружена: ждём условие, а не фиксированное время."""
    end = time.monotonic() + timeout
    while True:
        value = await check()
        if value or time.monotonic() > end:
            return value
        await asyncio.sleep(step)


async def saved(page, req, wid):
    """Дождаться, пока место вкладки ляжет на сервер."""
    async def check():
        return (await progress(req, wid))['locator'] == await page.evaluate(CFI)
    assert await until(check), ('позиция вкладки не дошла до сервера', await progress(req, wid), await page.evaluate(CFI))


async def at_cfi(page, want):
    async def check():
        return await page.evaluate(CFI) == want
    return await until(check)


async def main():
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.1)
    url = f'http://127.0.0.1:{port}'
    report = {}
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=['--no-sandbox'])
        prefs = "localStorage.setItem('reader.prefs', JSON.stringify({v:3, theme:'day', fontFamily:'system', fontScale:1, marginLevel:2, columns:1, flow:'paginated'}))"

        async def new_ctx():
            ctx = await browser.new_context(viewport={'width': 412, 'height': 915}, service_workers='block', base_url=url)
            await ctx.add_init_script(prefs)
            return ctx

        ctx = await new_ctx()
        req = ctx.request
        up = await req.post('/api/library/upload', multipart={'file': {'name': 'jumps.epub', 'mimeType': 'application/epub+zip', 'buffer': epub()}})
        assert up.ok, await up.text()
        wid = (await up.json())['id']

        page = await ctx.new_page()
        page.on('pageerror', lambda e: errors.append(f'[A] {e} {getattr(e, "stack", "")}'))
        await open_book(page, url, wid)

        print(len(errors), 'STEP 1 сноска', flush=True)
        await goto_chapter(page, 1)
        assert await until(lambda: section_is(page, 1))
        before = await page.evaluate(CFI)
        assert await history(req, wid) == [], 'чтение до главы не переход'
        await click_in_book(page, 'a[href="#fn1"]')
        await until(lambda: page.evaluate(f"async () => (await import('/js/core/state.js')).view.lastLocation.cfi !== {json.dumps(before)}"))
        await page.wait_for_timeout(800)
        at_note = await page.evaluate(CFI)
        assert at_note != before, 'клик по сноске не перешёл'
        rows = await history(req, wid)
        assert [r['reason'] for r in rows] == ['link'], rows
        assert rows[0]['book_rev'] and rows[0]['device'], rows[0]
        await back(page)
        assert await at_cfi(page, before), ('«Назад» после сноски не на исходном месте', await page.evaluate(CFI), before)
        assert len(await history(req, wid)) == 1, '«Назад» не пишет в историю'
        # Сноска → обратная ссылка: вершина снимается, новая запись не появляется.
        await click_in_book(page, 'a[href="#fn1"]')
        await page.wait_for_timeout(1500)
        await click_in_book(page, 'a[href="#fnref1"]')
        await page.wait_for_timeout(1500)
        assert await until(lambda: section_is(page, 1))
        report['footnote_back_exact_cfi'] = True

        print(len(errors), 'STEP 2 листание', flush=True)
        await req.delete(f'/api/progress/{wid}/history')
        await goto_chapter(page, 1)
        start = await page.evaluate(SECTION)
        for _ in range(120):
            cfi = await page.evaluate(CFI)
            await page.click('#next-btn')
            await until(lambda was=cfi: page.evaluate(f"async () => (await import('/js/core/state.js')).view.lastLocation.cfi !== {json.dumps(was)}"), timeout=5)
            if (await page.evaluate(SECTION)) >= start + 3:
                break
        assert await page.evaluate(SECTION) >= start + 3
        await page.wait_for_timeout(1500)
        assert await history(req, wid) == [], ('листание через главы попало в историю', await history(req, wid))
        report['paging_through_3_chapters_no_history'] = True

        print(len(errors), 'STEP 3 оглавление', flush=True)
        before = await page.evaluate(CFI)
        await page.click('#toc-btn')
        await page.click('#toc-list a:nth-child(2)')  # Глава 1
        await page.wait_for_timeout(1500)
        assert await until(lambda: section_is(page, 1))
        rows = await history(req, wid)
        assert [r['reason'] for r in rows] == ['toc'], rows
        await back(page)
        assert await at_cfi(page, before), ('«Назад» после оглавления не точен', await page.evaluate(CFI), before)
        assert len(await history(req, wid)) == 1
        report['toc_one_row_back_exact'] = True

        print(len(errors), 'STEP 4 две вкладки', flush=True)
        await req.delete(f'/api/progress/{wid}/history')
        await goto_chapter(page, 2)
        await saved(page, req, wid)
        place_a = await page.evaluate(CFI)
        page_b = await ctx.new_page()
        page_b.on('pageerror', lambda e: errors.append(f'[B] {e} {getattr(e, "stack", "")}'))
        await open_book(page_b, url, wid)
        assert await until(lambda: section_is(page_b, 2)), 'вторая вкладка открылась не на месте первой'
        await page_b.click('#toc-btn')
        await page_b.click('#toc-list a:nth-child(5)')  # Глава 4
        await page_b.wait_for_timeout(1000)
        await page_b.click('#next-btn')
        await saved(page_b, req, wid)
        # Возврат в первую вкладку.
        # Тост живёт 4 с, а машина тестов бывает медленной — собираем все тосты.
        await page.evaluate("""() => { window.__toasts = []; new MutationObserver(() => {
          const t = document.getElementById('toast-box')?.textContent || ''; if (t) window.__toasts.push(t)
        }).observe(document.body, { childList: true, subtree: true, characterData: true }) }""")
        # bring_to_front сам шлёт visibilitychange; событие ниже — на случай
        # headless-режима, где вкладки не скрываются.
        await page.bring_to_front()
        await page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        assert await until(lambda: section_is(page, 4)), 'нетронутая вкладка не перешла на свежее место'
        # Тост показывается после того, как переход устоялся (settle).
        assert await until(lambda: page.evaluate("() => window.__toasts.some(t => t.includes('Продолжаю с'))")), \
            ('нет тоста «Продолжаю с»', await page.evaluate('() => window.__toasts'))
        rows = await history(req, wid)
        assert [r['reason'] for r in rows] == ['toc'], ('переход на свежее место не пишет строк', rows)
        await back(page)
        assert await at_cfi(page, place_a), 'своё место вкладки не легло в «Назад»'
        await page_b.close()
        report['two_tabs_follow_with_toast'] = True

        print(len(errors), 'STEP 5 два устройства', flush=True)
        await req.delete(f'/api/progress/{wid}/history')
        await page.click('#next-btn')
        await saved(page, req, wid)
        ctx2 = await new_ctx()
        page2 = await ctx2.new_page()
        page2.on('pageerror', lambda e: errors.append(f'[2] {e} {getattr(e, "stack", "")}'))
        await open_book(page2, url, wid)
        await page2.click('#next-btn')
        await page2.click('#next-btn')
        await saved(page2, req, wid)
        assert await history(req, wid) == [], 'продолжение чтения на другом устройстве — не переход'
        dev2 = await page2.evaluate("() => JSON.parse(localStorage.getItem('reader.device.v1'))")
        dev1 = await page.evaluate("() => JSON.parse(localStorage.getItem('reader.device.v1'))")
        assert dev1['id'] != dev2['id']
        place2 = await page2.evaluate(CFI)
        # Первое устройство (вкладку не возвращали) листает со старой базой.
        await page.click('#next-btn')
        await saved(page, req, wid)
        rows = await history(req, wid)
        assert len(rows) == 1, rows
        assert (rows[0]['reason'], rows[0]['device'], rows[0]['device_id'], rows[0]['locator']) == ('device', dev2['name'], dev2['id'], place2), rows[0]
        # Подпись в панели.
        await page.evaluate("async () => { const j = await import('/js/jumps.js'); await j.renderJumpsPanel() }")
        panel = await page.evaluate("() => document.getElementById('jumps-list').textContent")
        assert f"другое устройство: {dev2['name']}" in panel, panel
        assert 'затёрто' not in panel
        report['two_devices_device_row'] = dev2['name']

        print(len(errors), 'STEP 6 офлайн', flush=True)
        await req.delete(f'/api/progress/{wid}/history')
        await ctx.set_offline(True)
        await page.click('#next-btn')
        await page.wait_for_timeout(1500)
        pending = await page.evaluate("() => JSON.parse(localStorage.getItem('reader.pendingProgress.v1') || '{}')")
        assert str(wid) in pending, 'без сети позиция должна лечь в очередь'
        offline_place = pending[str(wid)]['body']['locator']
        # Тем временем второе устройство читает дальше — его место новее.
        await page2.bring_to_front()
        await page2.click('#next-btn')
        await saved(page2, req, wid)
        newer = await progress(req, wid)
        assert newer['device_id'] == dev2['id']
        await page.bring_to_front()
        await ctx.set_offline(False)
        await until(lambda: page.evaluate(f"() => !(JSON.parse(localStorage.getItem('reader.pendingProgress.v1') || '{{}}'))['{wid}']"))
        pending = await page.evaluate("() => JSON.parse(localStorage.getItem('reader.pendingProgress.v1') || '{}')")
        assert str(wid) not in pending, ('досылка не ушла', pending)
        now = await progress(req, wid)
        assert now['locator'] == newer['locator'], 'старая офлайн-позиция затёрла более новую'
        rows = await history(req, wid)
        # Сверху — досланная офлайн-позиция первого устройства. Под ней — его же
        # место до отключения, которое второе устройство сдвинуло со своей
        # устаревшей базой (строка device): ни одна позиция не пропала.
        assert (rows[0]['reason'], rows[0]['locator'], rows[0]['device_id']) == ('offline', offline_place, dev1['id']), rows
        assert [r['reason'] for r in rows[1:]] == ['device'] and rows[1]['device_id'] == dev1['id'], rows
        report['offline_replay_older_logged'] = True

        await ctx2.close()
        # Известная гонка foliate (есть и на main e6714c2, не от #3239): таймер
        # ресайза paginator рендерит секцию, iframe которой уже выгружен при
        # быстром листании через границу главы. vendor/ не правим; любые другие
        # ошибки страницы валят тест.
        known = [e for e in errors if 'vendor/foliate-js/paginator.js' in e and (' at View.columnize' in e or ' at View.render' in e or 'reading \'document\'' in e)]
        unknown = [e for e in errors if e not in known]
        assert not unknown, '\n'.join(unknown)
        report['known_foliate_render_race_errors'] = len(known)
        print(json.dumps(report, ensure_ascii=False))
        await browser.close()
    server.should_exit = True


if __name__ == '__main__':
    asyncio.run(main())
