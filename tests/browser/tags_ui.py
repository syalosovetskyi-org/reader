"""Теги: настоящий фронтенд, API по docs/tags-api-contract.md мокается.

Запуск: python tests/browser/tags_ui.py (нужны Playwright и Chromium).
Прод и backend не используются. Проверяются десктоп и мобильный лист.
"""
import asyncio
import os
import threading
from copy import deepcopy
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
TAGS = [
    dict(id=1, name='Фэнтези', group='Жанр', count=3),
    dict(id=2, name='ЛитРПГ', group='Жанр', count=2),
    dict(id=3, name='Мироустройство', group='Темы и тропы', count=1),
    dict(id=4, name='Насилие', group='Контент', count=1),
    dict(id=5, name='Веб-сериал', group='Форма', count=1),
    dict(id=6, name='Ёлки', group='Темы и тропы', count=0),
]
WORKS = [
    dict(id=1, title='Первая книга', author='Автор', site='calibre',
         file_format='epub', write_status='done', write_status_source='site', tag_ids=[4, 3, 1, 2]),
    dict(id=2, title='Вторая книга', author='Другой', site='calibre',
         file_format='epub', write_status='wip', write_status_source='desc', tag_ids=[1, 2]),
    dict(id=3, title='Третья книга', author='Третий', site='upload',
         file_format='epub', write_status='dead', write_status_source='manual', tag_ids=[1]),
    dict(id=4, title='Без тегов', author='Автор', site='upload', file_format='epub',
         write_status='', write_status_source='', tag_ids=[]),
]


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'frontend'), **kwargs)

    def log_message(self, *args):
        pass


async def run(browser, url):
    works, tags = deepcopy(WORKS), deepcopy(TAGS)
    proposals = [dict(id=20, name='Киберсеть', group='Темы и тропы', count=2,
                      books=[dict(id=1, title='Первая книга'), dict(id=2, title='Вторая книга')])]
    requests, errors, warnings = [], [], []
    state = dict(fail_delete=False, fail_status=False, running=False)
    ctx = await browser.new_context(viewport={'width': 1200, 'height': 900}, service_workers='block')

    async def api(route):
        req = route.request
        parsed = urlsplit(req.url)
        path = parsed.path
        requests.append((req.method, path, req.post_data_json if req.post_data else None, parsed.query))
        payload = {}
        if path == '/api/tags':
            payload = tags
        elif path == '/api/tags/progress':
            payload = dict(total=4, tagged=3, by_step=dict(meta=1, desc=1, search=1, read=0),
                           queued=1, pending_proposals=len(proposals), running=state['running'])
        elif path == '/api/tags/proposals':
            payload = proposals
        elif path.startswith('/api/tags/proposals/'):
            action = path.rsplit('/', 1)[1]
            assert req.method == 'POST', req.method
            proposal = proposals.pop(0)
            if action == 'approve':
                tag = dict(id=proposal['id'], name=proposal['name'], group=proposal['group'], count=2)
                tags.append(tag)
                payload = dict(ok=True, tag=tag)
            else:
                payload = dict(ok=True)
        elif path == '/api/tags/run':
            assert parsed.query == 'step=2&limit=40'
            state['running'] = True
            payload = dict(started=True, queued=1)
        elif path == '/api/library':
            payload = [] if parsed.query else [
                {key: value for key, value in w.items() if key != 'write_status_source'} for w in works]
        elif path.startswith('/api/library/'):
            parts = path.split('/')
            work = next(w for w in works if w['id'] == int(parts[3]))

            def book_tags():
                return [{**{key: t[key] for key in ('id', 'name', 'group')}, 'source': 'manual' if t['id'] == 6 else 'meta'}
                        for t in tags if t['id'] in work['tag_ids']]

            if len(parts) == 4:
                payload = {**work, 'tags': book_tags(), 'description': 'Описание книги'}
            elif parts[4] == 'tags':
                if req.method == 'DELETE':
                    if state['fail_delete']:
                        await route.fulfill(status=503, json=dict(detail='нет связи'))
                        return
                    work['tag_ids'].remove(int(parts[5]))
                elif req.method == 'POST':
                    work['tag_ids'].append(req.post_data_json['tag_id'])
                payload = book_tags()
            elif parts[4] == 'write-status':
                if state['fail_status']:
                    await route.fulfill(status=503, json=dict(detail='нет связи'))
                    return
                work['write_status'] = req.post_data_json['status']
                work['write_status_source'] = 'manual'
                payload = {key: work[key] for key in ('write_status', 'write_status_source')}
        elif path in ('/api/monitored', '/api/calibre/books', '/api/progress/finished'):
            payload = []
        elif path == '/api/ingest/jobs':
            payload = dict(active=0, jobs=[])
        elif path.endswith('/toc'):
            payload = dict(items=[])
        elif path.endswith('/cover'):
            await route.fulfill(status=404)
            return
        await route.fulfill(json=payload)

    await ctx.route('**/api/**', api)
    page = await ctx.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: warnings.append(message.text) if message.type == 'warning' else None)
    await page.goto(url)
    cards = page.locator('#book-grid .book-card')
    await expect(cards).to_have_count(4)
    await expect(page.locator('#lib-tag-count')).to_have_text('4 из 4 книг')
    assert await page.locator('.book-main-tags').first.locator('span').all_text_contents() == ['Фэнтези', 'ЛитРПГ']

    # Страница книги: группы идут в порядке словаря, происхождение в title.
    await page.locator('.book-card').filter(has_text='Первая книга').locator('.b-title').click()
    await expect(page.locator('#bp-tags [data-book-tag]')).to_have_count(4)
    assert await page.locator('#bp-tags .tag-group-label').all_text_contents() == ['Жанр', 'Темы и тропы', 'Контент']
    await expect(page.locator('#bp-body .tag-status')).to_have_text('● Дописана')
    await expect(page.locator('#bp-body .tag-status')).to_have_attribute('title', 'Сайт')
    await expect(page.locator('#bp-tags [data-book-tag="1"]')).to_have_attribute('title', 'Метаданные')
    await expect(page.locator('#bp-tags [data-book-tag="1"]')).to_have_class('tag-chip c-genre')
    await expect(page.locator('#bp-tags [data-book-tag="4"]')).to_have_class('tag-chip c-warn')
    await page.locator('#bp-tags [data-book-tag="1"]').click()
    await expect(page.locator('#library')).to_be_visible()
    await expect(cards).to_have_count(3)

    # Статус И тег, живой текстовый поиск, счётчики и скрытие нулевых тегов.
    await page.locator('#lib-status-segment [data-tag-status="wip"]').click()
    await expect(cards).to_have_count(1)
    await expect(page.locator('#lib-tag-count')).to_have_text('1 из 4 книг')
    await page.fill('#lib-q', 'невозможно')
    await expect(cards).to_have_count(0)
    await page.fill('#lib-q', 'Вторая')
    await expect(cards).to_have_count(1)
    await page.fill('#lib-q', '')
    await page.locator('#lib-tags-btn').click()
    await expect(page.locator('#lib-tags-btn')).to_have_attribute('aria-expanded', 'true')
    await expect(page.locator('#lib-tag-groups [data-tag-id="2"] .tag-n')).to_have_text('1')
    await expect(page.locator('#lib-tag-groups [data-tag-id="4"]')).to_have_count(0)
    await page.keyboard.press('Escape')
    await expect(page.locator('#lib-tags-btn')).to_be_focused()
    assert sum(method == 'GET' and path == '/api/tags' for method, path, _, _ in requests) == 1

    # Состояние фильтра переживает перезагрузку.
    await page.reload()
    await expect(cards).to_have_count(1)
    await expect(page.locator('#lib-status-segment [data-tag-status="wip"]')).to_have_attribute('aria-pressed', 'true')
    await page.locator('#lib-status-segment [data-tag-status=""]').click()
    await page.locator('.book-card').filter(has_text='Первая книга').locator('.b-title').click()
    await expect(page.locator('#bp-tags [data-book-tag]')).to_have_count(4)
    await page.locator('#bp-tags [data-edit]').click()
    await page.locator('#bp-tags [data-book-tag="2"]').click()
    await expect(page.locator('#bp-tags [data-book-tag="2"]')).to_have_count(0)
    assert ('DELETE', '/api/library/1/tags/2', None, '') in requests
    await page.locator('#bp-tags [data-add]').click()
    await page.locator('#bp-tags [data-tag-query]').fill('елК')
    await expect(page.locator('#bp-tags [data-choose-tag]')).to_have_count(1)
    await page.locator('#bp-tags [data-choose-tag="6"]').click()
    await expect(page.locator('#bp-tags [data-book-tag="6"]')).to_be_visible()
    assert ('POST', '/api/library/1/tags', {'tag_id': 6}, '') in requests
    await page.locator('.tag-status-edit select').select_option('dead')
    await expect(page.locator('.tag-status-edit select')).to_have_value('dead')
    assert ('PUT', '/api/library/1/write-status', {'status': 'dead'}, '') in requests

    # Неуспех не меняет книгу, пишет logErr и toast; повторное действие работает.
    state['fail_delete'] = True
    await page.locator('#bp-tags [data-book-tag="6"]').click()
    await expect(page.locator('.toast-err').last).to_contain_text('Не удалось удалить тег')
    await expect(page.locator('#bp-tags [data-book-tag="6"]')).to_be_visible()
    state['fail_delete'] = False
    state['fail_status'] = True
    await page.locator('.tag-status-edit select').select_option('done')
    await expect(page.locator('.toast-err').last).to_contain_text('Не удалось изменить статус')
    await expect(page.locator('.tag-status-edit select')).to_have_value('dead')
    state['fail_status'] = False
    assert any('Не удалось удалить тег' in warning for warning in warnings)
    await page.locator('#bp-tags [data-edit]').click()
    await expect(page.locator('#bp-body .tag-status')).to_have_attribute('title', 'Вручную')
    await page.locator('#bp-back').click()
    await expect(page.locator('#library')).to_be_visible()

    # Пустая книга: есть вход в правку, нет плашки неизвестного статуса.
    await page.locator('#lib-picked-tags button').click()
    await page.locator('.book-card').filter(has_text='Без тегов').locator('.b-title').click()
    await expect(page.locator('#bp-tags')).to_contain_text('Теги ещё не проставлены')
    await expect(page.locator('#bp-body .tag-status')).to_have_count(0)
    await page.locator('#bp-back').click()

    # Очередь из «Ещё»: approve строго по контракту, затем пустое состояние.
    await page.locator('#more-actions-btn').click()
    await expect(page.locator('#tags-pending-count')).to_have_text('· 1')
    await page.locator('#tags-admin-btn').click()
    await expect(page.locator('#tags-admin-progress')).to_contain_text('размечено 3 из 4')
    await expect(page.locator('[data-proposal="20"]')).to_contain_text('Киберсеть')
    await page.locator('[data-proposal="20"] [data-action="approve"]').click()
    await expect(page.locator('[data-proposal="20"]')).to_have_class('tag-proposal gone')
    await expect(page.locator('.tag-proposal-result')).to_have_text('«Киберсеть» добавлен в словарь')
    assert ('POST', '/api/tags/proposals/20/approve', {}, '') in requests
    await page.locator('#tags-run').click()
    await expect(page.locator('#tags-run')).to_have_text('Разметка идёт…')
    assert ('POST', '/api/tags/run', {}, 'step=2&limit=40') in requests
    await page.keyboard.press('Escape')
    await page.locator('#tags-admin-btn').click()
    await expect(page.locator('#tags-proposals')).to_have_text('Новых тегов нет')
    await page.keyboard.press('Escape')

    # Телефон: черновик не меняет выдачу, Escape отменяет, «Показать» применяет.
    await page.set_viewport_size({'width': 390, 'height': 844})
    await expect(cards).to_have_count(4)
    await page.locator('#lib-filter-mobile').click()
    await page.locator('#lib-sheet-status [data-tag-status="wip"]').click()
    await page.locator('#lib-tag-groups [data-tag-id="1"]').click()
    await expect(page.locator('#lib-tag-apply')).to_have_text('Показать 1')
    await expect(cards).to_have_count(4)
    await page.keyboard.press('Escape')
    await expect(page.locator('#lib-filter-mobile')).to_be_focused()
    await expect(page.locator('#lib-filter-mobile')).to_have_text('Фильтр · 0')
    await page.locator('#lib-filter-mobile').click()
    await page.locator('#lib-sheet-status [data-tag-status="wip"]').click()
    await page.locator('#lib-tag-groups [data-tag-id="1"]').click()
    await page.locator('#lib-tag-apply').click()
    await expect(cards).to_have_count(1)
    await expect(page.locator('#lib-filter-mobile')).to_have_text('Фильтр · 2')
    await page.locator('#lib-filter-mobile').click()
    await page.locator('#lib-tag-reset').click()
    await expect(cards).to_have_count(1)
    await expect(page.locator('#lib-tag-apply')).to_have_text('Показать 4')
    # Все цели нажатия в листе имеют высоту не меньше 44px.
    assert await page.locator('#lib-tag-picker').evaluate(
        "el => [...el.querySelectorAll('button')].filter(x => x.getClientRects().length).every(x => x.getBoundingClientRect().height >= 44)")
    await page.locator('#lib-tag-apply').click()
    await expect(cards).to_have_count(4)
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
    await ctx.close()
    print('tags_ui: ok (книга, И-фильтр, поиск, правка, ошибки, очередь, мобильный лист)')


async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async with async_playwright() as playwright:
            options = dict(args=['--no-sandbox'])
            if os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
                options['executable_path'] = os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
            browser = await playwright.chromium.launch(**options)
            try:
                await run(browser, f'http://127.0.0.1:{server.server_port}')
            finally:
                await browser.close()
    finally:
        server.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
