# Контракт API тегов (serg/tasks#3285) — общий для бэкенда и фронтенда

Уже есть (этап 1, ветка feat/tags-3285):
- GET  /api/tags → [{id, name, group, count}] — активные теги в порядке словаря; count — видимых книг.
- GET  /api/library → у каждой книги добавлены `write_status` ('' | done | wip | dead) и `tag_ids` [int].
- GET  /api/library/{id} → `tags`: [{id, name, group, source}] (source: meta|desc|search|read|manual), `write_status`.

Добавляется (этап 2):
- GET  /api/library/{id} → ещё `write_status_source` ('' | meta | desc | site | search | read | manual).
- POST   /api/library/{id}/tags        body {tag_id}  → ручное добавление (source=manual, снимает removed). Ответ: tags книги как в GET.
- DELETE /api/library/{id}/tags/{tag_id}              → ручное удаление: removed=true (автоматика больше не ставит). Ответ: tags книги.
- PUT    /api/library/{id}/write-status body {status: ''|done|wip|dead} → source=manual. Ответ {write_status, write_status_source}.
- GET  /api/tags/proposals → [{id, name, group, count, books: [{id, title}] (до 5)}] — state=proposed, по убыванию count.
- POST /api/tags/proposals/{id}/approve            → state=active; его WorkTag становятся видимыми. Ответ {ok, tag}.
- POST /api/tags/proposals/{id}/merge body {into_tag_id} → имя предложения уходит в синонимы целевого тега, WorkTag переносятся на целевой, state=merged. Ответ {ok}.
- POST /api/tags/proposals/{id}/reject             → state=rejected; WorkTag удаляются; модель этот тег больше не предлагает. Ответ {ok}.
- GET  /api/tags/progress → {total, tagged, by_step: {meta, desc, search, read}, queued, pending_proposals, running: bool}
  — total видимых книг; tagged — у кого есть хоть один видимый тег; by_step — сколько книг остановились на этом шаге (tag_step);
  queued — книги, которым ещё нужен шаг 2.
- POST /api/tags/run?step=2&limit=N → запустить фоновую разметку (не ждёт). Ответ {started: bool, queued}.
