"""Издания одной книги в живом Chromium + foliate + НАСТОЯЩИЙ бэкенд (serg/tasks#3240).

Прод не трогается: сервер поднимается во временном каталоге данных (SQLite).
Run with Playwright installed: python tests/browser/editions.py

1. RU и EN издания связаны; EN дочитан до середины 4-й главы. Быстрый RU↔EN
   открывает RU в той же главе («Глава 4») и показывает тост с «Вернуть».
2. «Вернуть» возвращает RU на собственное место.
3. Разделённый экран открывается (две foliate-view, вторая идёт следом в ту же
   главу) и закрывается — читалка остаётся на ведущем издании.
4. Широкий экран: озвучка/перевод/закладки/подсветки — иконками в тулбаре;
   «Перевести» скрыт (книга русская / есть русское издание).
5. Телефон 390×844: тулбар в одну строку, под текстом ≥ 85 % высоты.
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
_tmp = tempfile.mkdtemp(prefix='reader-browser-editions-')
for k, v in {'READER_DATA_DIR': _tmp, 'READER_BOOKS_DIR': f'{_tmp}/books', 'READER_COVERS_DIR': f'{_tmp}/covers',
             'READER_TMP_DIR': f'{_tmp}/tmp', 'READER_TTS_DIR': f'{_tmp}/tts',
             'READER_SECRET_KEY_PATH': f'{_tmp}/secret.key', 'READER_DB_PATH': f'{_tmp}/reader.db'}.items():
    os.environ[k] = v
os.environ.pop('READER_DB_URL', None)
os.environ['READER_ALLOW_SQLITE'] = '1'  # только временный SQLite, прод-БД не трогаем
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright  # noqa: E402

CH = 6


def epub(lang, label, word):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr('book.opf', f'<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">ed-{lang}</dc:identifier><dc:title>Unsong {lang}</dc:title><dc:creator>Scott Alexander</dc:creator><dc:language>{lang}</dc:language></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' + ''.join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(CH)) + '</manifest><spine>' + ''.join(f'<itemref idref="c{i}"/>' for i in range(CH)) + '</spine></package>')
        z.writestr('nav.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>' + ''.join(f'<li><a href="c{i}.xhtml">{label} {i + 1}</a></li>' for i in range(CH)) + '</ol></nav></body></html>')
        for i in range(CH):
            body = f'<h1>{label} {i + 1}</h1>' + ''.join(f'<p>{word} {i + 1}.{j:02d} ' + (word + ' ') * 60 + '</p>' for j in range(24))
            z.writestr(f'c{i}.xhtml', f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{label} {i + 1}</title></head><body>{body}</body></html>')
    return buf.getvalue()


def start_server():
    import uvicorn

    from backend.app import scheduler
    scheduler.start = lambda: None
    scheduler.shutdown = lambda: None
    from backend.app.main import app

    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    return server, f'http://127.0.0.1:{port}'


LOC = """async () => { const s = await import('/js/core/state.js'); const l = s.view?.lastLocation
  return l ? { label: l.tocItem?.label || '', section: l.section?.current, cfi: l.cfi, work: s.currentWork?.id } : null }"""
PREFS = "localStorage.setItem('reader.prefs', JSON.stringify({v:3, theme:'day', fontFamily:'system', fontScale:1, marginLevel:2, columns:1, flow:'paginated'}))"


async def wait_label(page, work_id, label, timeout=15000):
    await page.wait_for_function(
        """async ([w, l]) => { const s = await import('/js/core/state.js'); const x = s.view?.lastLocation
           return s.currentWork?.id === w && x?.tocItem?.label === l }""", arg=[work_id, label], timeout=timeout)


async def main():
    import httpx

    server, url = start_server()
    with httpx.Client(base_url=url) as c:
        ru = c.post('/api/library/upload', files={'file': ('unsong-ru.epub', epub('ru', 'Глава', 'слово'), 'application/epub+zip')}).json()['id']
        en = c.post('/api/library/upload', files={'file': ('unsong-en.epub', epub('en', 'Chapter', 'word'), 'application/epub+zip')}).json()['id']
        r = c.post(f'/api/editions/{ru}/link', json={'other_id': en})
        assert r.status_code == 200, r.text
        assert {e['language'] for e in r.json()['editions']} == {'ru', 'en'}

    report, errors, vendor_races = {}, [], []

    def on_error(e):
        stack = e.stack or ''
        # Известные гонки foliate (vendor/, править нельзя): отложенный на 80 мс
        # render() после ресайза застаёт секцию без документа, а fonts.ready
        # срабатывает уже после destroy(). Падение — в самом paginator.js (первый
        # кадр стека). Не молчим: число попаданий печатается в отчёте.
        top = next((ln for ln in stack.splitlines() if ln.strip().startswith('at ')), '')
        if 'vendor/foliate-js/paginator.js' in top:
            vendor_races.append(list(report)[-1:])
            return
        errors.append(f'{e} [после шага: {list(report)[-1:]}] @ {stack[:600]}')
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=['--no-sandbox'])
        ctx = await browser.new_context(viewport={'width': 1280, 'height': 800}, service_workers='block')
        await ctx.add_init_script(PREFS)
        page = await ctx.new_page()
        page.on('pageerror', on_error)
        logs = []
        page.on('console', lambda m: logs.append(f'{m.type}: {m.text}'))
        await page.goto(f'{url}/?open={en}')
        await page.wait_for_function("async () => (await import('/js/core/state.js')).view?.lastLocation?.cfi")
        await page.wait_for_selector('#ed-quick:not([hidden])')

        # --- 4. широкий экран: иконки в тулбаре, перевод скрыт (есть RU) ---
        vis = await page.evaluate("""() => Object.fromEntries(['#tts-wide','#tr-wide','#bm-wide','#hl-wide','#more-btn','#split-btn']
            .map(s => [s, !!document.querySelector(s)?.offsetWidth]))""")
        assert vis['#tts-wide'] and vis['#bm-wide'] and vis['#hl-wide'] and vis['#split-btn'], vis
        assert not vis['#tr-wide'], ('у книги есть русское издание — «Перевести» не нужен', vis)
        assert not vis['#more-btn'], ('«Ещё» на широком экране не нужен', vis)
        report['wide_icons'] = True

        # --- 1. EN: середина главы 4 → быстрый переход на RU ---
        # serg/tasks#3239: позиция пишется только после действия человека —
        # сначала «касание», потом листание (как у position_restore.py HUMAN).
        await page.evaluate("() => window.dispatchEvent(new PointerEvent('pointerdown'))")
        await page.evaluate("async () => { const v = (await import('/js/core/state.js')).view; await v.goTo('c3.xhtml'); await v.next(); await v.next() }")
        await page.wait_for_timeout(1500)
        own_ru = None
        quick = await page.inner_text('#ed-quick')
        assert 'EN→RU' in quick.replace(' ', ''), quick
        await page.click('#ed-quick')
        await wait_label(page, ru, 'Глава 4')
        try:
            await page.wait_for_selector('#ed-toast:not([hidden])', timeout=5000)
        except Exception:
            print('\n'.join(logs[-30:]))
            print(await page.evaluate("() => [document.getElementById('ed-toast').outerHTML, document.getElementById('ed-chip').outerHTML]"))
            raise
        toast = await page.inner_text('#ed-toast')
        assert 'Открыто на том же месте' in toast and 'Глава 4' in toast and 'Вернуть' in toast, toast
        report['switch_same_chapter'] = (await page.evaluate(LOC))['label']

        # --- 2. «Вернуть» — на своё место RU (начало книги: RU ещё не читали) ---
        await page.click('#ed-toast [data-act="action"]')
        await page.wait_for_timeout(1200)
        own_ru = await page.evaluate(LOC)
        assert own_ru['label'] != 'Глава 4', ('«Вернуть» не вернул', own_ru)
        report['undo'] = own_ru['label']
        # Обратно в главу 4 для разделённого экрана.
        await page.evaluate("() => window.dispatchEvent(new PointerEvent('pointerdown'))")
        await page.evaluate("async () => { const v = (await import('/js/core/state.js')).view; await v.goTo('c3.xhtml') }")
        await wait_label(page, ru, 'Глава 4')

        # --- 3. разделённый экран ---
        await page.click('#split-btn')
        await page.wait_for_function("""() => { const v = [...document.querySelectorAll('#split foliate-view')]
            return v.length === 2 && v.every(x => x.lastLocation?.tocItem) }""", timeout=20000)
        await page.wait_for_timeout(800)
        labels = await page.evaluate("() => [...document.querySelectorAll('#split foliate-view')].map(v => v.lastLocation.tocItem.label)")
        assert labels == ['Глава 4', 'Chapter 4'], ('вторая половина не пошла следом', labels)
        lead_tag = await page.inner_text('#split-body .col.is-lead .tag')
        assert 'RU' in lead_tag, lead_tag
        # Листаем вторую (EN) — она становится ведущей, RU идёт следом.
        # Действие человека во второй половине делает её ведущей (не relocate).
        await page.evaluate("""() => { const v = document.querySelectorAll('#split foliate-view')[1]
            for (const c of v.renderer.getContents()) c.doc.dispatchEvent(new PointerEvent('pointerdown')) }""")
        await page.evaluate("async () => { const v = document.querySelectorAll('#split foliate-view')[1]; await v.goTo('c4.xhtml') }")
        await page.wait_for_function("() => document.querySelectorAll('#split foliate-view')[0].lastLocation?.tocItem?.label === 'Глава 5'", timeout=8000)
        assert 'EN' in await page.inner_text('#split-body .col.is-lead .tag')
        report['split_follow'] = True
        await page.click('#split-exit')
        await wait_label(page, en, 'Chapter 5')
        assert await page.evaluate("() => document.getElementById('split').hidden && !document.getElementById('reader').hidden")
        assert await page.evaluate("() => document.querySelectorAll('#split foliate-view').length") == 0
        report['split_exit_on_lead'] = True

        # --- 6. страница книги: блок «Издания», связь, отвязка с подтверждением ---
        await page.evaluate(f"async () => {{ const m = await import('/js/book-page.js'); await m.openBookPage({{ id: {ru} }}) }}")
        await page.wait_for_selector('#bp-editions .ed-list .ed >> nth=1', timeout=15000)
        block = await page.inner_text('#bp-editions')
        assert 'Издания' in block and 'Прогресс у каждого свой' in block and 'Синхронизировать позицию' in block, block
        assert await page.get_attribute('#bp-editions .sw', 'aria-checked') == 'true'
        await page.click('#bp-editions [data-act="unlink"] >> nth=1')
        assert 'Книга и прогресс останутся' in await page.inner_text('#bp-editions .confirm')
        await page.click('#bp-editions [data-act="unlink-no"]')
        assert await page.locator('#bp-editions .confirm').count() == 0
        await page.click('#bp-editions [data-act="linkp"]')
        await page.wait_for_selector('#bp-editions .ed-panel input[placeholder="Название или автор"]')
        await page.click('#bp-editions [data-act="linkx"]')
        report['book_page_block'] = True

        # --- 5. телефон 390×844 ---
        phone = await browser.new_context(viewport={'width': 390, 'height': 844}, service_workers='block',
                                          is_mobile=True, has_touch=True)
        await phone.add_init_script(PREFS)
        pp = await phone.new_page()
        pp.on('pageerror', on_error)
        await pp.goto(f'{url}/?open={ru}')
        await pp.wait_for_function("async () => (await import('/js/core/state.js')).view?.lastLocation?.cfi")
        await pp.wait_for_selector('#ed-switch:not([hidden])')
        await pp.wait_for_timeout(800)
        geo = await pp.evaluate("""() => {
          const r = (s) => document.querySelector(s)?.getBoundingClientRect()
          const top = r('#reader-top'), host = r('#view-host')
          const btns = [...document.querySelectorAll('#reader-top button')].filter(b => b.offsetWidth)
          return { host: host.height, vh: innerHeight, topH: top.height,
                   maxRight: Math.max(...btns.map(b => b.getBoundingClientRect().right)),
                   rows: new Set(btns.map(b => { const q = b.getBoundingClientRect(); return Math.round((q.top + q.bottom) / 2 / 8) })).size,
                   title: r('#reader-title').width,
                   btns: btns.map(b => [b.id || b.className, Math.round(b.getBoundingClientRect().left), Math.round(b.getBoundingClientRect().width), Math.round(b.getBoundingClientRect().top)]) }
        }""")
        share = geo['host'] / geo['vh']
        assert share >= 0.85, ('под текстом меньше 85 %', geo)
        assert geo['maxRight'] <= 390 and geo['rows'] == 1, ('тулбар не помещается в строку', geo)
        assert geo['title'] >= 32, ('заголовок главы сжат в ноль', geo)
        report['phone_text_share'] = round(share, 3)
        # Меню издания на телефоне: в нём «Два издания рядом».
        await pp.click('#ed-switch')
        assert 'Два издания рядом' in await pp.inner_text('#ed-menu')
        await pp.click('#ed-menu [data-act="split"]')
        await pp.wait_for_function("() => document.querySelectorAll('#split foliate-view').length === 2 && document.getElementById('split-body').classList.contains('rows')", timeout=20000)
        report['phone_split_rows'] = True
        await pp.click('#split-exit')
        await pp.wait_for_function("() => document.getElementById('split').hidden")

        assert not errors, errors
        report['vendor_paginator_race'] = len(vendor_races)
        print(json.dumps(report, ensure_ascii=False))
        await browser.close()
    server.should_exit = True


if __name__ == '__main__':
    asyncio.run(main())
