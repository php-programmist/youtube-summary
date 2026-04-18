# План: YouTube Playlist → Telegram Summary (n8n + Ollama)

## Context

Нужен локальный сервис, который раз в час проверяет публичный YouTube-плейлист через RSS, для каждого нового видео получает субтитры через supadata.ai, строит обзор (8–12 пунктов) и главную идею на русском через локальную Ollama, и шлёт по одному сообщению на видео в Telegram. Требование PRD: модель грузится только когда появились субтитры и выгружается после отправки отчёта. Пакетная обработка за один цикл. Исходная точка — пустой репозиторий (`prd.md` + `.idea/`), стек выбирает пользователь: n8n + Ollama + supadata.ai в docker-compose на WSL2 с NVIDIA GPU 16+ GB.

## Итоговые решения (из уточнений)

- YouTube: публичный плейлист, источник списка — RSS `https://www.youtube.com/feeds/videos.xml?playlist_id={PLAYLIST_ID}` (без API-ключа).
- Telegram: токен и `chat_id` уже есть → в `.env`.
- supadata.ai: ключ уже есть → в `.env`. База `https://api.supadata.ai/v1`, заголовок `x-api-key`.
- Ollama: внутри docker-compose с прокидом NVIDIA GPU. Модель по умолчанию `qwen2.5:14b-instruct-q4_K_M` (≈9 GB VRAM, сильный русский). `OLLAMA_MODEL` — переопределяемая переменная `.env`.
- Отчёт: всегда на русском, одно сообщение на видео, средняя длина ~1200 симв. (8–12 пунктов + главная идея).
- Состояние: SQLite `state.db` в volume данных n8n (`/home/node/.n8n/state.db`), доступ из Code node через `better-sqlite3` (`NODE_FUNCTION_ALLOW_EXTERNAL=better-sqlite3`).
- Нет субтитров → короткое уведомление в Telegram, видео помечается обработанным (чтобы не повторять).
- Первый запуск: обрабатывается весь набор из RSS (обычно последние ~15 видео).
- Расписание: Cron раз в час + Manual Trigger для теста.
- При падении workflow — Error Trigger шлёт алерт в тот же Telegram-чат.

## Архитектура

```
                 ┌──────────────────────────────────────────┐
                 │  docker-compose (host: WSL2 + NVIDIA)    │
                 │                                          │
 Schedule(1h) ──▶│   n8n  ──▶ RSS ──▶ SQLite diff ──▶ loop: │
 Manual Trigger  │            supadata → Ollama → Telegram  │
                 │   ollama (GPU) ◀────── HTTP ─────────────│
                 │                                          │
                 │   volumes: n8n_data, ollama_models       │
                 └──────────────────────────────────────────┘
                       ▲                       │
                       │                       ▼
                supadata.ai API          Telegram Bot API
```

## Структура репозитория

```
yt-summary/
├── docker-compose.yml
├── .env.example              # шаблон секретов / настроек
├── .env                      # локальный, в .gitignore
├── .gitignore
├── README.md                 # запуск, настройка, траблшутинг
├── workflows/
│   ├── yt-summary-hourly.json    # экспорт основного workflow
│   └── yt-summary-on-error.json  # экспорт error workflow
├── ollama/
│   └── init-model.sh         # one-shot pull дефолтной модели
└── data/                     # bind-volume (в .gitignore)
    ├── n8n/                  # .n8n, credentials, state.db
    └── ollama/               # модели
```

## Prerequisites (однократно на хосте WSL)

1. Docker Desktop с включённой WSL Integration (или Docker Engine в WSL).
2. Свежий NVIDIA Windows-драйвер (поддержка CUDA on WSL).
3. NVIDIA Container Toolkit внутри WSL:
   ```bash
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
     sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
     sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo service docker restart   # или перезапуск Docker Desktop
   ```
4. Проверка: `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.

## docker-compose.yml (ключевые части)

```yaml
services:
  n8n:
    image: n8nio/n8n:latest
    restart: unless-stopped
    ports: ["5678:5678"]
    environment:
      - N8N_HOST=${N8N_HOST:-localhost}
      - N8N_PORT=5678
      - N8N_PROTOCOL=http
      - GENERIC_TIMEZONE=${TZ:-Europe/Moscow}
      - TZ=${TZ:-Europe/Moscow}
      - N8N_SECURE_COOKIE=false
      - N8N_RUNNERS_ENABLED=true
      - NODE_FUNCTION_ALLOW_EXTERNAL=better-sqlite3
    volumes:
      - ./data/n8n:/home/node/.n8n
    depends_on:
      ollama:
        condition: service_healthy

  ollama:
    image: ollama/ollama:latest
    restart: unless-stopped
    volumes:
      - ./data/ollama:/root/.ollama
    environment:
      - OLLAMA_KEEP_ALIVE=5m       # совместимо с явными keep_alive в запросах
    healthcheck:
      test: ["CMD", "ollama", "list"]
      interval: 10s
      timeout: 5s
      retries: 10
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

  ollama-init:
    image: ollama/ollama:latest
    depends_on:
      ollama:
        condition: service_healthy
    environment:
      - OLLAMA_HOST=http://ollama:11434
      - OLLAMA_MODEL=${OLLAMA_MODEL:-qwen2.5:14b-instruct-q4_K_M}
    entrypoint: ["/bin/sh","-c","ollama pull $${OLLAMA_MODEL}"]
    restart: "no"
```

Примечания:
- `n8n` ходит в `http://ollama:11434` по внутренней сети compose.
- GPU прокидывается только для `ollama` и `ollama-init`.
- `ollama-init` один раз подтягивает модель и выходит (экономит ручные шаги).

## .env.example

```
# YouTube
YOUTUBE_PLAYLIST_ID=PLxxxxxxxxxxxxxxx

# Telegram
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=123456789

# supadata.ai
SUPADATA_API_KEY=sd_...

# Ollama
OLLAMA_MODEL=qwen2.5:14b-instruct-q4_K_M
OLLAMA_KEEP_ALIVE_ACTIVE=10m

# n8n
TZ=Europe/Moscow
N8N_HOST=localhost
```

Значения подтягиваются в workflow через `{{$env.XXX}}`.

## Основной workflow: `yt-summary-hourly`

Узлы по порядку:

1. **Triggers (оба в одном workflow):**
   - `Schedule Trigger` — cron `0 * * * *` (каждый час, TZ по `GENERIC_TIMEZONE`).
   - `Manual Trigger` — для теста.
   Оба ведут в один и тот же следующий узел.

2. **HTTP Request — RSS плейлиста**
   - Method `GET`
   - URL `https://www.youtube.com/feeds/videos.xml?playlist_id={{$env.YOUTUBE_PLAYLIST_ID}}`
   - Response format `String` (XML).

3. **XML** — распарсить RSS в объект.

4. **Code (JS) — нормализовать список**
   - Извлечь `feed.entry` → массив `{ videoId, title, publishedAt, url }`.
   - `videoId` = `entry."yt:videoId"`.

5. **Code (JS) — SQLite: отфильтровать необработанные**
   ```js
   const Database = require('better-sqlite3');
   const db = new Database('/home/node/.n8n/state.db');
   db.exec(`CREATE TABLE IF NOT EXISTS processed_videos (
     video_id TEXT PRIMARY KEY,
     title TEXT,
     processed_at TEXT DEFAULT (datetime('now')),
     status TEXT,
     lang TEXT
   )`);
   const stmt = db.prepare('SELECT 1 FROM processed_videos WHERE video_id = ?');
   return items.filter(i => !stmt.get(i.json.videoId));
   ```
   Возврат: только новые видео.

6. **IF** — если массив пуст → конец workflow (nothing to do).

7. **Loop Over Items** (batch size 1) — дальше по одному видео:

   7.1 **HTTP Request — supadata transcript**
   - `GET https://api.supadata.ai/v1/youtube/transcript`
   - Query: `videoId={{$json.videoId}}`, `text=true` (plain text).
   - Header: `x-api-key: {{$env.SUPADATA_API_KEY}}`.
   - Option: `Continue On Fail = true` (обрабатываем 404/невалидные отдельно).

   7.2 **IF — есть ли субтитры**
   - True (HTTP 200 и `content` непустой): → 7.3.
   - False: →
     - **Telegram — "субтитров нет"**: сообщение `Субтитров нет: <a href="https://youtu.be/{videoId}">{title}</a>`.
     - **Code — пометить processed со статусом `no_transcript`**.
     - → следующая итерация.

   7.3 **HTTP Request — Ollama chat (грузит модель при первом вызове)**
   - `POST http://ollama:11434/api/chat`
   - JSON body:
     ```json
     {
       "model": "{{$env.OLLAMA_MODEL}}",
       "stream": false,
       "format": "json",
       "keep_alive": "{{$env.OLLAMA_KEEP_ALIVE_ACTIVE}}",
       "options": { "temperature": 0.3, "num_ctx": 16384 },
       "messages": [
         {"role":"system","content":"<SYSTEM_PROMPT>"},
         {"role":"user","content":"Название: {{$node['Loop'].json.title}}\n\nСубтитры:\n{{$json.content}}"}
       ]
     }
     ```
   - SYSTEM_PROMPT (жёстко задан в узле):
     ```
     Ты ассистент, который кратко пересказывает видео на YouTube по его субтитрам.
     Всегда отвечай на русском языке.
     Верни ТОЛЬКО JSON вида:
     {"main_idea": "<1-2 предложения с главной идеей>", "summary": ["пункт 1", "пункт 2", ...]}
     Требования:
     - 8-12 пунктов в summary;
     - каждый пункт ≤ 120 символов, законченная мысль;
     - без вступлений, без markdown, только JSON.
     ```
   - Если субтитры длиннее контекста — обрезать в предшествующем Code node до ~40k символов (эвристика под num_ctx=16k токенов).

   7.4 **Code — распарсить ответ**
   - Вынуть `message.content`, `JSON.parse` → `{ main_idea, summary[] }`.
   - Защита: если parse упал → fallback: `summary = [first 10 lines of raw]`, `main_idea = first sentence`.

   7.5 **Telegram — отправка отчёта**
   - Chat ID: `{{$env.TELEGRAM_CHAT_ID}}`.
   - Parse mode: `HTML`.
   - Текст:
     ```
     <b>{{title}}</b>
     <a href="https://youtu.be/{{videoId}}">Смотреть на YouTube</a>

     📌 <b>Главная идея</b>
     {{main_idea}}

     📝 <b>Обзор</b>
     • {{summary[0]}}
     • {{summary[1]}}
     ...
     ```
   - HTML-экранирование `title`, `main_idea`, пунктов (`<`, `>`, `&`) — в отдельном Code node перед Telegram.

   7.6 **Code — пометить processed со статусом `summarized` и `lang` из supadata ответа**.

8. **HTTP Request — выгрузить модель Ollama**
   - Ставится ПОСЛЕ `Loop Over Items` (то есть после отправки всех сообщений).
   - `POST http://ollama:11434/api/chat`
   - Body: `{"model": "{{$env.OLLAMA_MODEL}}", "messages": [], "keep_alive": 0}`.
   - Это выгружает модель из VRAM (PRD-требование). Если модель и не загружалась (все видео без субтитров или нет новых) — запрос безопасный no-op.

## Error workflow: `yt-summary-on-error`

- `Error Trigger` (привязан к `yt-summary-hourly`).
- `Telegram` → `chat_id` = `{{$env.TELEGRAM_CHAT_ID}}`, текст:
  ```
  ⚠️ yt-summary упал
  Узел: {{$json.execution.error.node.name}}
  Сообщение: {{$json.execution.error.message}}
  Execution ID: {{$json.execution.id}}
  ```

## SQLite схема

```sql
CREATE TABLE IF NOT EXISTS processed_videos (
  video_id     TEXT PRIMARY KEY,
  title        TEXT,
  processed_at TEXT DEFAULT (datetime('now')),
  status       TEXT CHECK(status IN ('summarized','no_transcript','error')),
  lang         TEXT
);
CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_videos(processed_at);
```

Инициализация — при первом вызове в шаге 5 (idempotent `CREATE IF NOT EXISTS`).

## Критические файлы / артефакты к созданию

- `docker-compose.yml` — оркестрация n8n + ollama + ollama-init.
- `.env.example`, `.env`, `.gitignore` — конфигурация и секреты.
- `workflows/yt-summary-hourly.json` — экспорт основного workflow.
- `workflows/yt-summary-on-error.json` — экспорт error workflow.
- `ollama/init-model.sh` — опциональный shell для ручного pull.
- `README.md` — инструкция запуска, переменные, траблшутинг WSL/GPU.
- `data/` — в `.gitignore`.

## Первый запуск

1. `cp .env.example .env` и заполнить значения.
2. `docker compose up -d` — поднимает ollama, подтягивает модель, запускает n8n.
3. Открыть `http://localhost:5678`, создать владельца.
4. Импортировать оба workflow из `workflows/*.json`.
5. Создать credentials: Telegram (токен), HTTP header auth для supadata (`x-api-key`).
6. Активировать оба workflow.
7. Запустить `yt-summary-hourly` вручную — в Telegram должны прийти сообщения по всем видео из RSS.

## Верификация (E2E)

- Happy path: вручную запустить workflow при пустой `state.db` → ожидаются ~15 сообщений в Telegram, каждое с заголовком, ссылкой, главной идеей, 8–12 пунктами.
- Дедупликация: повторный запуск сразу после — 0 новых сообщений.
- Нет субтитров: подставить `videoId` заведомо без субтитров (короткий Shorts без CC) → одно короткое уведомление.
- Выгрузка модели: `docker exec $(docker ps -qf name=ollama) ollama ps` после workflow → список пустой (либо скоро станет пустым).
- GPU работает: во время ответа Ollama `nvidia-smi` в WSL показывает занятую память процессом `ollama`.
- Расписание: после активации ждать час или временно выставить `*/5 * * * *` для проверки и вернуть `0 * * * *`.
- Ошибка: временно сломать `SUPADATA_API_KEY` → прилетает алерт от error-workflow.

## Известные ограничения / к решению при эксплуатации

- Длинные субтитры (> ~40k симв.) обрезаются — для 1–2-часовых лекций можно позже добавить map-reduce по чанкам.
- Telegram лимит 4096 символов: при превышении шаблон режется. Текущий бюджет ~1200 симв. оставляет запас.
- RSS отдаёт обычно последние 15 видео плейлиста — более старые видео, добавленные после 15-го, не будут обнаружены. Если это важно, перейти на `GET /v1/youtube/playlist/videos` supadata.
- Модель и её pull (≈9 GB) случаются при первом старте — объяснить в README.
