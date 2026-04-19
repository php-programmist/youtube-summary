# Multi-User Workflow — Design Spec

**Дата:** 2026-04-19
**Область:** `workflows/`, `scripts/bootstrap.sh`, `README.md`, `.gitignore`

## Цель

Расширить существующий workflow `yt-summary-hourly` так, чтобы он мог обслуживать несколько пользователей одновременно: для каждого — свой YouTube-плейлист и свой Telegram chat. Конфигурация пользователей живёт в файле, добавление нового — одна строка в JSON без редактирования workflow.

## Решения (зафиксированные в ходе брейншторминга)

| Вопрос | Решение |
|---|---|
| Где хранить список пользователей | Файл `workflows/state/users.json` (в `.gitignore`) |
| Схема записи пользователя | `{ "name": "...", "playlistId": "...", "chatId": "..." }` — только три поля |
| Где хранить состояние «обработано» | `workflows/state/processed.json` — плоско, без привязки к плейлисту (как сейчас) |
| Миграция существующего `processed.json` | Не требуется — формат не меняется |
| Куда слать ошибки | В `TELEGRAM_CHAT_ID` из `.env` — он становится админским каналом |
| Архитектурный подход | Sub-workflow: главный — триггер + loop по юзерам; sub-workflow — обработка одного пользователя |

## Архитектура

### Файловая структура после изменений

```
workflows/
├─ yt-summary-hourly.json         ← модифицирован: триггер + loop по юзерам + Execute Workflow
├─ yt-summary-per-user.json       ← НОВЫЙ: sub-workflow обработки одного пользователя
├─ yt-summary-on-error.json       ← БЕЗ ИЗМЕНЕНИЙ (использует TELEGRAM_CHAT_ID)
└─ state/
   ├─ processed.json              ← без изменений (плоский словарь videoId → title)
   └─ users.json                  ← НОВЫЙ (в .gitignore)
```

### Главный workflow: `yt-summary-hourly.json`

Остаются:
- `Schedule Trigger` (cron `0 * * * *`)
- `Manual Trigger`

Новые/заменяющие ноды:
1. **Code - Load Users** — читает `/workflows/state/users.json`, возвращает по одному item на пользователя (`{name, playlistId, chatId}`). Если файл отсутствует или невалиден — выбрасывает понятную ошибку (её подхватит `yt-summary-on-error` и зальёт в админский `TELEGRAM_CHAT_ID`).
2. **Loop Over Users** (`Split In Batches`) — итерируется по пользователям, batchSize = 1.
3. **Execute Workflow** — вызывает `yt-summary-per-user` по его ID, передаёт текущий item пользователя.
4. После sub-workflow — возврат в `Loop Over Users` (ветка loop).

Удаляются (переезжают в sub-workflow): весь граф от `HTTP Request - RSS Playlist` до `Code - Mark Processed *`.

**⚠ Внимание к `splitInBatches v3`** (из auto-memory): в v3 выходы переставлены — index 0 = done, index 1 = loop. Соединения `Loop Over Users` должны это учитывать.

### Sub-workflow: `yt-summary-per-user.json`

Триггер: **Execute Workflow Trigger**, принимает inputs `name`, `playlistId`, `chatId`.

Далее — ровно текущий граф `yt-summary-hourly`, с тремя точечными заменами:

| Нода | Было | Станет |
|---|---|---|
| `HTTP Request - RSS Playlist` | URL: `...playlist_id={{$env.YOUTUBE_PLAYLIST_ID}}` | URL: `...playlist_id={{$('Execute Workflow Trigger').first().json.playlistId}}` |
| `Telegram - Send Summary` | chatId: `={{$env.TELEGRAM_CHAT_ID}}` | chatId: `={{$('Execute Workflow Trigger').first().json.chatId}}` |
| `Telegram - No Transcript` | chatId: `={{$env.TELEGRAM_CHAT_ID}}` | chatId: `={{$('Execute Workflow Trigger').first().json.chatId}}` |

Ноды `Code - Filter New`, `Code - Mark Processed Summarized`, `Code - Mark Processed No Transcript` — без изменений (общий `processed.json`).

Sub-workflow наследует активный error-workflow (или глобально настроенный), так что ошибки внутри sub-workflow прилетят в админский чат через `yt-summary-on-error`.

### Error workflow: `yt-summary-on-error.json`

Без изменений. Продолжает слать в `TELEGRAM_CHAT_ID` — который теперь де-факто админский канал.

## Схема `users.json`

```json
[
  {
    "name": "default",
    "playlistId": "PLxxxxxxxxxxxxxxx",
    "chatId": "123456789"
  },
  {
    "name": "bob",
    "playlistId": "PLyyyyyyyyyyyyyyy",
    "chatId": "987654321"
  }
]
```

- **`name`** — строка, человекочитаемое имя. Используется только в логах/ошибках.
- **`playlistId`** — ID YouTube-плейлиста (как в `.env`).
- **`chatId`** — строка (важно: Telegram API принимает и строку, и число; в текущем workflow используется строка).

Файл читается при каждом запуске workflow — достаточно сохранить изменения, рестарт n8n не нужен.

## Изменения в `scripts/bootstrap.sh`

После существующей инициализации `workflows/state/processed.json`:

```bash
if [ ! -f workflows/state/users.json ]; then
  : "${YOUTUBE_PLAYLIST_ID:?YOUTUBE_PLAYLIST_ID must be set in .env}"
  : "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID must be set in .env}"
  cat > workflows/state/users.json <<EOF
[
  {
    "name": "default",
    "playlistId": "${YOUTUBE_PLAYLIST_ID}",
    "chatId": "${TELEGRAM_CHAT_ID}"
  }
]
EOF
  echo "  ✓ created workflows/state/users.json for default user"
fi
```

`chown -R 1000:1000 workflows/state` в существующем `case` покрывает новый файл — дополнительной логики не нужно.

Идемпотентность сохраняется: если файл уже есть — не трогаем.

## Изменения в `.gitignore`

Добавить строку:

```
workflows/state/users.json
```

## Изменения в README

Новая секция после «Настройка» (или перед «Первичное развертывание workflow'ов»):

### «Несколько пользователей»

Краткий текст:
- где живёт файл (`workflows/state/users.json`)
- формат (пример из спецификации)
- как добавить нового: дописать объект в массив, сохранить
- важное: каждый `chatId` должен хотя бы раз инициировать диалог с ботом (Telegram quirk) — иначе `Forbidden: bot can't initiate conversation`
- для отключения пользователя — удалить/закомментировать строку (JSON-комментарии n8n не парсит — удалять)
- рестарт не нужен, следующий запуск по расписанию подхватит

Также обновить таблицу в «Структура репо», добавив `yt-summary-per-user.json` и `users.json`.

## Риски и нюансы

1. **n8n ID для Execute Workflow.** Execute Workflow привязывается к sub-workflow по ID. Поскольку `scripts/n8n.sh` импортирует workflow с ID из JSON (как уже делается для error-workflow), достаточно жёстко зашить `id` sub-workflow (`yt-summary-per-user.json`) и прописать его в Execute Workflow ноде главного workflow.
2. **Один токен бота — много чатов.** `TELEGRAM_BOT_TOKEN` остаётся один; каждый новый `chatId` должен инициировать диалог с ботом первым сообщением, иначе Telegram API ответит ошибкой.
3. **Общий `processed.json` между пользователями.** Если один и тот же видео-ID попадёт в два разных плейлиста — второй пользователь его не получит (детерминировано). Для текущего use case это приемлемо (подтверждено).
4. **Частичные отказы в loop.** Если обработка одного пользователя упадёт — Execute Workflow пробросит ошибку, и Loop Over Users остановится. Чтобы ошибка одного пользователя не блокировала остальных, на Execute Workflow включаем `continueOnFail: true`. Ошибка всё равно прилетит в on-error workflow через sub-workflow и попадёт в админский чат.
5. **TELEGRAM_CHAT_ID в .env больше не «чат отчётов».** Он становится админским каналом. В `.env.example` желательно обновить комментарий у переменной, чтобы не было двусмысленности.

## YAGNI — что НЕ делаем

- Нет `enabled` флага в `users.json` — отключение удалением строки.
- Нет per-user настроек LLM / языка / тона.
- Нет миграции `processed.json` — формат не меняется.
- Нет отдельного `TELEGRAM_ADMIN_CHAT_ID` — переиспользуем существующий.
- Нет UI/admin-тулы — файл правится редактором.

## Verification-чек-лист для реализатора

1. `./scripts/bootstrap.sh` на чистой системе создаёт `workflows/state/users.json` с одной записью из `.env`.
2. `./scripts/bootstrap.sh` повторно — `users.json` не перезаписывается.
3. `./scripts/n8n.sh push-all` импортирует оба основных workflow (hourly + per-user) без ручной перепривязки.
4. Ручной запуск `yt-summary-hourly` при одном пользователе в `users.json` → в Telegram приходит ровно тот же набор сообщений, что и до рефакторинга (регрессии нет).
5. Добавили второго пользователя (другой `chatId` и `playlistId`) → следующий ручной запуск → сообщения по его плейлисту приходят в его чат; первому пользователю — по его плейлисту в его чат.
6. Повторный запуск → 0 новых сообщений (`processed.json` работает).
7. Сломанный `playlistId` у одного из юзеров → остальные пользователи обрабатываются, админ получает уведомление от `yt-summary-on-error`.
8. `workflows/state/users.json` корректно игнорируется git (`git status` не показывает).

## Out of scope

- Web UI управления пользователями.
- База данных вместо файла.
- Per-user модели / настройки суммаризации.
- Авторизация / разграничение доступа.
