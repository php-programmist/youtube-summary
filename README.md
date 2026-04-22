# yt-summary

Локальный сервис: раз в час проверяет YouTube-плейлисты через RSS, для каждого нового видео получает субтитры через supadata.ai, суммирует локальной Ollama (8–12 пунктов + главная идея) и шлёт отчёт в Telegram. Поддерживает несколько пользователей — каждый со своим плейлистом и Telegram-чатом. Модель загружается в VRAM только при наличии субтитров и выгружается после отправки всех сообщений. Дедупликация — через общий `workflows/state/processed.json`.

## Стек

- **n8n** — оркестрация workflow
- **Ollama** (Docker, NVIDIA GPU) — локальный LLM, по умолчанию `gemma4:e4b` (~11 GB VRAM)
- **supadata.ai** — получение субтитров YouTube
- **Docker Compose** на WSL2 + NVIDIA GPU (16+ GB VRAM рекомендуется)

## Prerequisites

### YouTube
Создать публичный плейлист (или доступный только по ссылке) на YouTube и получить его ID из URL `?list=PLxxxxxxxxxxxxxxx`.

Задать сортировку: **Дате добавления: сначала новые**. Без этого workflow будет видеть только первые 15 видео плейлиста по порядку добавления.

### Telegram
- Создать бота через @BotFather и получить его токен. 
- Получить `chat_id` чата/канала для отчётов - Зайдите в Настройки → Продвинутые настройки → Экспериментальные настройки. Включите опцию Show Peer IDs in Profile, после чего ID будет отображаться в профиле чата.

### Supadata.ai
- Получить API-ключ [supadata.ai](https://supadata.ai).

### Docker

Docker Desktop с включённой WSL Integration или Docker Engine напрямую в WSL.

### NVIDIA-драйвер

Актуальный NVIDIA Windows-драйвер с поддержкой CUDA on WSL (Game Ready / Studio ≥ 520).

### NVIDIA Container Toolkit

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo service docker restart   # или перезапустить Docker Desktop
```

Проверка:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

### Внешние сервисы

- Telegram-бот через @BotFather + `chat_id` чата/канала.
- API-ключ [supadata.ai](https://supadata.ai).
- ID публичного YouTube-плейлиста — из URL вида `?list=PLxxxxxxxxxxxxxxx`.

## Настройка

```bash
cp .env.example .env
```

Заполнить `.env`:

| Переменная | Описание | Пример |
|---|---|---|
| `YOUTUBE_PLAYLIST_ID` | ID плейлиста из URL `?list=...` | `PLxxxxxxxxxxxxxxx` |
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather | `123456:AA...` |
| `TELEGRAM_CHAT_ID` | ID чата/канала для ошибок (админский; per-user чаты — в `users.json`) | `123456789` |
| `SUPADATA_API_KEY` | API-ключ supadata.ai | `sd_...` |
| `OLLAMA_MODEL` | Модель Ollama (переопределяемая) | `gemma4:e4b` |
| `OLLAMA_KEEP_ALIVE_ACTIVE` | Время удержания модели в VRAM во время работы | `10m` |
| `TZ` | Временная зона для расписания n8n | `Europe/Moscow` |
| `N8N_HOST` | Хост n8n (менять только при проксировании) | `localhost` |

## Запуск

Первый запуск — через bootstrap-скрипт:

```bash
./scripts/bootstrap.sh
```

Скрипт:

1. Поднимает `ollama` с доступом к GPU и ждёт healthcheck.
2. Проверяет модель в volume `yt-summary_ollama-data`; если её нет — пулит (`~9.6 GB`, несколько минут, зависит от канала).
3. Стартует `n8n` (состояние — в docker-managed named volume `yt-summary_n8n-data`, права настраиваются автоматически).

Скрипт идемпотентен: при повторном запуске модель не перекачивается, уже работающие контейнеры не пересоздаются.

Последующие запуски (после выключения):

```bash
docker compose up -d
```

После каждого `docker compose up` sidecar-контейнер `n8n-kicker` дожидается healthy-состояния n8n и дёргает webhook `yt-summary-hourly`, так что свежие видео из плейлиста забираются сразу, без ожидания почасового cron. Сам kicker завершается после одного запроса (`docker compose ps` покажет его как `exited (0)`).

Открыть [http://localhost:5678](http://localhost:5678) и создать учётную запись владельца.

## Выбор LLM

Проведен бенчмарк из 7 локальных моделей (Ollama, RTX 3080 Laptop 16 ГБ, транскрипт 29 929 символов, по 3 прогона). 

1) Score — композитная метрика (структура JSON + длины + покрытие ключевых слов + специфичность).
2) Judge (0–10) — экспертная оценка связности и фактической точности пересказа.
3) Inference time (с) — отражает «чистую» скорость модели на генерации summary, независимо от размера промпта и холодного старта.

Топ-4 по эффективности (сочетание Judge, скорости и потребления VRAM):

| Ранг | Модель | Judge | Score | Inference | VRAM | Size | Когда использовать |
|:---:|---|:---:|---:|---:|---:|---:|---|
| 1 | `gemma4:e4b` | **9** | 84.4 | 9.0 с | 11.3 ГБ | 10.6 ГБ | **Дефолт для прод-сценария.** Лучшее качество пересказа: корректные названия, последовательность шагов, стабильность между прогонами. Быстрая (79 tok/s). |
| 2 | `qwen2.5:14b-instruct-q4_K_M` | 8 | 79.3 | 106.8 с | 13.1 ГБ | 17.6 ГБ | **Когда важно качество, но не время.** Офлайн/батч-обработка, когда модель уже в VRAM и inference-время не критично. Не годится для почасового workflow: 1–2 минуты на видео. |
| 3 | `phi4:14b` | 7 | 75.9 | 23.9 с | 13.1 ГБ | 13.7 ГБ | **Компромисс при VRAM ≥ 14 ГБ.** Разумная связность при средней скорости. Вариант fallback на gemma4, если она недоступна или даёт OOM. |
| 4 | `qwen2.5:7b-instruct-q4_K_M` | 6 | 71.7 | 5.8 с | 7.4 ГБ | 7.8 ГБ | **Ограниченный бюджет VRAM (≤ 8 ГБ).** Самая быстрая из пригодных: годится для старых GPU и при массовых прогонах. Возможны локальные галлюцинации и редкие вкрапления нелатиницы — это цена компактности. |

Не рекомендуются: `phi4-mini:3.8b` (битый JSON, выдуманные фразы), `llama3.1:8b` (зацикливается в пересказе), `llama3.2:3b` (поверхностный пересказ с мешаниной языков).

Модель задаётся переменной `OLLAMA_MODEL` в `.env`; `scripts/bootstrap.sh` подтянет её при первом запуске. Чтобы сменить модель — обновите `.env` и перезапустите скрипт: он увидит, что новой модели нет в volume, и выполнит pull.

## Несколько пользователей

Конфигурация пользователей хранится в `workflows/state/users.json` (не в git). Файл создаётся автоматически при первом запуске `./scripts/bootstrap.sh` с одним пользователем «default» из `.env`.

Формат файла:

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

**Добавить нового пользователя** — дописать объект в массив и сохранить файл. Рестарт n8n не нужен: при следующем запуске по расписанию файл перечитывается заново.

**Важно:** каждый `chatId` должен хотя бы раз инициировать диалог с ботом первым сообщением — иначе Telegram API ответит `Forbidden: bot can't initiate conversation`.

**Отключить пользователя** — удалить его объект из массива (JSON-комментарии не поддерживаются).

**Ошибки** (в том числе для конкретного пользователя) уходят в `TELEGRAM_CHAT_ID` из `.env` через `yt-summary-on-error` — он становится де-факто админским каналом.

## Первичное развертывание workflow'ов

Workflow'ы лежат в `workflows/` как JSON и деплоятся через n8n CLI внутри контейнера — без импорта через UI. Каталог `./workflows` смонтирован в контейнер как `/workflows` (см. `docker-compose.yml`).

```bash
./scripts/n8n.sh push-all
```

Команда создаёт оба workflow в n8n c id и связями из JSON. Ссылка между `yt-summary-hourly` и `yt-summary-on-error` (error workflow) уже зашита в JSON по id — руками связывать ничего не нужно.

### Credentials (разово через UI)

CLI n8n не даёт создавать credentials со всеми полями, поэтому — один раз в UI:

1. В n8n создать credential типа **Telegram API**, поле **Access Token** = `TELEGRAM_BOT_TOKEN`.
2. Открыть `yt-summary-per-user` → узлы Telegram → выбрать созданный credential. То же для `yt-summary-on-error`.
3. Сохранить новый id credential в git:
   ```bash
   ./scripts/n8n.sh pull
   git add workflows/ && git commit -m "link telegram credential"
   ```

### Активировать

```bash
./scripts/n8n.sh activate-all
```

## Изменение workflow

Вариант A — через n8n UI (правка в редакторе):

```bash
./scripts/n8n.sh pull            # забрать изменения из n8n в workflows/
git diff workflows/              # проверить diff
git add workflows/ && git commit
```

Вариант B — через JSON-файлы (для ИИ-агентов и правок вслепую):

```bash
$EDITOR workflows/yt-summary-hourly.json
./scripts/n8n.sh push yt-summary-hourly
```

`push` всегда деактивирует workflow в n8n (поведение CLI) и сразу восстанавливает состояние `active` из JSON, так что активные остаются активными.

### Команды `scripts/n8n.sh`

| Команда | Что делает |
|---|---|
| `pull` | Экспортирует все workflow из n8n, нормализует (убирает волатильные поля, сортирует ключи), переименовывает по `name` |
| `push <slug>` | Импортирует `workflows/<slug>.json` в n8n и синхронизирует флаг `active` |
| `push-all` | Импортирует все `workflows/*.json` |
| `list` | Список workflow в n8n (`id|name`) |
| `activate <slug>` | Активирует workflow по id из JSON |
| `activate-all` | Активирует все workflow в n8n |

## Первый запуск

После `push-all` + `activate-all` достаточно `docker compose up -d` — sidecar `n8n-kicker` сам дёрнет webhook `yt-summary-hourly`. RSS возвращает обычно последние ~15 видео плейлиста — по каждому в Telegram придёт сообщение с главной идеей и кратким обзором.

Альтернативно — запустить `yt-summary-hourly` вручную кнопкой **Execute workflow** в UI.

Повторный запуск сразу после → 0 сообщений: все видео уже помечены обработанными в `workflows/state/processed.json`.

## Верификация

| Сценарий | Ожидаемый результат |
|---|---|
| Первый `docker compose up` при пустой БД | ~15 сообщений в Telegram (kicker дёрнул webhook) |
| `docker compose ps` после старта | `n8n-kicker` в статусе `exited (0)` |
| Повторный запуск сразу после | 0 новых сообщений |
| Видео без субтитров | Короткое уведомление «Субтитров нет» |
| Выгрузка модели | `docker exec ollama ollama ps` — список пуст после завершения workflow |
| GPU | `nvidia-smi` во время работы Ollama — занятая VRAM процессом ollama |
| Расписание | После активации ждать 1 час или временно выставить `*/5 * * * *` для проверки |
| Ошибка workflow | Сломать `SUPADATA_API_KEY` → прилетает Telegram-алерт от `yt-summary-on-error` |

## Траблшутинг

**GPU не виден в Ollama**
Проверить, что `nvidia-ctk runtime configure --runtime=docker` выполнен и Docker перезапущен. Верифицировать: `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.

**Бэкап / восстановление состояния n8n**
Данные лежат в docker-managed volume, а не в папке проекта. Бэкап:
```bash
docker run --rm -v yt-summary_n8n-data:/d -v "$(pwd)":/b alpine tar czf /b/n8n-backup.tgz -C /d .
```
Восстановление:
```bash
docker compose down
docker volume rm yt-summary_n8n-data
docker volume create yt-summary_n8n-data
docker run --rm -v yt-summary_n8n-data:/d -v "$(pwd)":/b alpine tar xzf /b/n8n-backup.tgz -C /d
docker compose up -d
```

**Ollama OOM / "model requires more system memory"**
Переопределить модель в `.env`: `OLLAMA_MODEL=gemma4:e4b`, затем повторно запустить `./scripts/bootstrap.sh` — он увидит отсутствие модели в volume и выполнит pull.

**Длинные субтитры обрезаются**
Текущий лимит ~40k символов перед подачей в Ollama. Видео длиннее ~1.5 часа будет суммировано частично.

**RSS отдаёт только ~15 видео**
Ограничение YouTube RSS-ленты. Более старые видео плейлиста не попадут в выборку. При необходимости перейти на endpoint `GET /v1/youtube/playlist/videos` supadata.ai.

**Telegram 400 Bad Request**
Обычно — невалидный HTML в тексте сообщения (неэкранированные `<`, `>`, `&`). Шаблон в узле уже экранирует заголовки; если редактируете шаблон — не забывайте об этом.

**`n8n-kicker` завершается с ошибкой**
Контейнер `n8n-kicker` ретраит webhook (`--retry 10 --retry-all-errors`) и падает только если n8n так и не ответил 200. Частая причина — `yt-summary-hourly` не активирован, поэтому production-webhook `yt-summary-kick` не зарегистрирован. Проверить: `./scripts/n8n.sh list` → в колонке `active` должно быть `true`; если нет — `./scripts/n8n.sh activate-all`.

## Переменные окружения

Все переменные описаны в разделе [Настройка](#настройка). Значения подтягиваются в workflow через `{{$env.VARIABLE_NAME}}`.

## Структура репо

```
yt-summary/
├── docker-compose.yml
├── .env.example
├── .env                          # локальный, в .gitignore
├── .gitignore
├── README.md
├── workflows/                    # bind-mounted в n8n как /workflows
│   ├── yt-summary-hourly.json    # триггеры (cron + webhook) + loop по пользователям
│   ├── yt-summary-per-user.json  # sub-workflow обработки одного пользователя
│   ├── yt-summary-on-error.json  # алерт в админский Telegram при ошибке
│   └── state/
│       ├── processed.json        # дедупликация (в .gitignore)
│       └── users.json            # список пользователей (в .gitignore)
└── scripts/
    ├── bootstrap.sh              # первый запуск: ollama + pull модели + n8n (идемпотентно)
    ├── n8n.sh                    # pull/push/activate workflow'ов через CLI n8n
    └── n8n_sync.py               # нормализация JSON (стабильные git-diff'ы)
```

Состояние n8n и модели Ollama хранятся в docker-managed named volumes `yt-summary_n8n-data` и `yt-summary_ollama-data` — не в каталоге проекта. Список volumes: `docker volume ls | grep yt-summary`.
