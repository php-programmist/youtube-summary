# yt-summary

Локальный сервис: раз в час проверяет YouTube-плейлист через RSS, для каждого нового видео получает субтитры через supadata.ai, суммирует локальной Ollama (8–12 пунктов + главная идея) и шлёт отчёт в Telegram. Модель загружается в VRAM только при наличии субтитров и выгружается после отправки всех сообщений. Дедупликация — через workflow staticData n8n.

## Стек

- **n8n** — оркестрация workflow
- **Ollama** (Docker, NVIDIA GPU) — локальный LLM, по умолчанию `qwen2.5:14b-instruct-q4_K_M` (~9 GB VRAM)
- **supadata.ai** — получение субтитров YouTube
- **Docker Compose** на WSL2 + NVIDIA GPU (16+ GB VRAM рекомендуется)

## Prerequisites

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
| `TELEGRAM_CHAT_ID` | ID чата/канала для отчётов | `123456789` |
| `SUPADATA_API_KEY` | API-ключ supadata.ai | `sd_...` |
| `OLLAMA_MODEL` | Модель Ollama (переопределяемая) | `qwen2.5:14b-instruct-q4_K_M` |
| `OLLAMA_KEEP_ALIVE_ACTIVE` | Время удержания модели в VRAM во время работы | `10m` |
| `TZ` | Временная зона для расписания n8n | `Europe/Moscow` |
| `N8N_HOST` | Хост n8n (менять только при проксировании) | `localhost` |

## Запуск

```bash
docker compose up -d --build
```

Флаг `--build` нужен только при первом запуске (или после изменения `Dockerfile`) — локально собирается образ `yt-summary-n8n:local` поверх `n8nio/n8n:latest`.

При первом старте:

1. `ollama` запускается с доступом к GPU.
2. `ollama-init` однократно скачивает модель (~9 GB) — первый запуск займёт несколько минут в зависимости от канала.
3. `n8n` стартует после того, как `ollama` прошёл healthcheck. Состояние n8n хранится в docker-managed named volume `yt-summary_n8n-data` (владелец/права настраиваются автоматически — ничего руками делать не нужно).

Открыть [http://localhost:5678](http://localhost:5678) и создать учётную запись владельца.

## Импорт workflow'ов

1. В n8n: нажать "+" → Workflows → "..." → Import from file .
2. Импортировать `workflows/yt-summary-hourly.json`.
3. Импортировать `workflows/yt-summary-on-error.json`.

### Credentials

#### Создать credential типа **Telegram API**:

- Поле **Access Token**: значение `TELEGRAM_BOT_TOKEN`.

### Связать error workflow

Открыть `yt-summary-on-error` и нажать кнопку "Publish". Без этого не получится его выбрать в качестве error workflow.

В настройках `yt-summary-hourly` → "..." → **Settings** → поле **Error Workflow** → выбрать `yt-summary-on-error`.

### Активировать

Включить оба workflow тумблером (верхний правый угол в редакторе).

## Первый запуск

Запустить `yt-summary-hourly` вручную кнопкой **Execute workflow**. RSS возвращает обычно последние ~15 видео плейлиста — по каждому в Telegram придёт сообщение с главной идеей и кратким обзором.

Повторный ручной запуск сразу после → 0 сообщений: все видео уже помечены обработанными в `staticData` workflow (хранится в БД n8n внутри named volume `yt-summary_n8n-data`).

## Верификация

| Сценарий | Ожидаемый результат |
|---|---|
| Первый ручной запуск при пустой БД | ~15 сообщений в Telegram |
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
Переопределить модель в `.env`: `OLLAMA_MODEL=qwen2.5:7b-instruct-q4_K_M`, затем `docker compose restart`. `ollama-init` подтянет новую модель автоматически (повторный pull).

**Длинные субтитры обрезаются**
Текущий лимит ~40k символов перед подачей в Ollama. Видео длиннее ~1.5 часа будет суммировано частично.

**RSS отдаёт только ~15 видео**
Ограничение YouTube RSS-ленты. Более старые видео плейлиста не попадут в выборку. При необходимости перейти на endpoint `GET /v1/youtube/playlist/videos` supadata.ai.

**Telegram 400 Bad Request**
Обычно — невалидный HTML в тексте сообщения (неэкранированные `<`, `>`, `&`). Шаблон в узле уже экранирует заголовки; если редактируете шаблон — не забывайте об этом.

## Переменные окружения

Все переменные описаны в разделе [Настройка](#настройка). Значения подтягиваются в workflow через `{{$env.VARIABLE_NAME}}`.

## Структура репо

```
yt-summary/
├── Dockerfile                    # кастомный образ n8n (точка расширения)
├── docker-compose.yml
├── .env.example
├── .env                          # локальный, в .gitignore
├── .gitignore
├── README.md
├── workflows/
│   ├── yt-summary-hourly.json
│   └── yt-summary-on-error.json
├── ollama/
│   └── init-model.sh
└── data/                         # bind-volume только для ollama (модели)
    └── ollama/
```

Состояние n8n хранится в docker-managed named volume `yt-summary_n8n-data` — не в каталоге проекта. Список volumes: `docker volume ls | grep yt-summary`.
