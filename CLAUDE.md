# CLAUDE.md

Команды смотри в `scripts/n8n.sh` и `scripts/bootstrap.sh`, файлы — `ls workflows/ scripts/`, env — `.env`.

## n8n

- **Switch v3**: выходы перевёрнуты относительно splitInBatches v3 — `index 0 = done`, `index 1 = loop`. Уже учтено в JSON, не «чини».
- **`import:workflow` всегда деактивирует** воркфлоу. Поэтому `n8n.sh` после импорта читает `active` из JSON и зовёт `publish:workflow` / `unpublish:workflow` (замена deprecated `update:workflow`; warning про рестарт misleading — изменения применяются сразу).
- **Порядок активации**: `yt-summary-on-error` → `yt-summary-per-user` (sub-workflow) → `yt-summary-hourly` (оркестратор). `cmd_activate_all` это соблюдает.
- **`n8n_sync.py` переписывает `workflows/*.json` при `pull`** (убирает volatile-поля, сортирует ключи). Не правь JSON «наполовину» — либо целиком в файле и `push-all`, либо в UI и `pull`.

## Инварианты архитектуры

- **n8n JSON — source of truth.** Каноническая копия в `workflows/*.json`, не в БД n8n. Меняя `active` в JSON, помни: `push-all` применит через publish/unpublish.
- **State на хосте, не в git.** `workflows/state/{users,processed}.json` bind-mount'ятся в контейнер; `bootstrap.sh` создаёт их из `.env`. n8n в контейнере = uid 1000, поэтому `chown` обязателен.
- **Ollama из n8n** дёргается по `http://ollama:11434` (docker network), а не через `OLLAMA_HOST`.
- **Python в `scripts/` намеренно stdlib only** — не добавляй `requirements.txt` без веской причины.
