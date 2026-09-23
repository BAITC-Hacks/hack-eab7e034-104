# StockPilot AI — HackAlem AI 2026 / Logistics

Веб-приложение для автоматического формирования рекомендаций по закупкам поставщикам для кейса ТОО «Электрокомплект».

## Что уже готово

- веб-dashboard закупщика;
- детерминированный расчёт replenishment;
- сезонность и устойчивый рост спроса;
- оценка упущенного спроса при stockout;
- робастная фильтрация разовых выбросов;
- остаток + товар в пути + MOQ;
- приоритет дефицита и risk score;
- объяснение каждой рекомендации и calculation trace;
- группировка по поставщикам через данные результата;
- CSV export;
- AI-агент с tool calling;
- безопасный fallback без OpenAI API key;
- подтверждение менеджером перед экспортом, без автоматической отправки поставщику;
- `/health` endpoint для cloud health checks.

## Архитектура

`Browser → FastAPI → deterministic replenishment engine → optional OpenAI agent`

LLM **не считает закупку самостоятельно**. Он вызывает инструменты, получает результаты расчётного ядра и объясняет их менеджеру.

## Быстрый запуск локально

```bash
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Открыть: http://127.0.0.1:8000

Проверка health: http://127.0.0.1:8000/health

## Онлайн-деплой — Render

Проект подготовлен для Render через `render.yaml`.

### Вариант A — через GitHub

1. Создайте обычный GitHub repository (не Codespaces).
2. Загрузите содержимое этого проекта в repository.
3. Откройте Render → **New → Web Service**.
4. Подключите GitHub repository.
5. Render может взять настройки из `render.yaml`. Если форма просит значения вручную:
   - Runtime: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Health Check Path: `/health`
6. Нажмите **Create Web Service**.
7. После успешного deploy Render выдаст публичный `onrender.com` адрес.

Render официально поддерживает FastAPI как Web Service и использует команду запуска, слушающую `0.0.0.0` и `$PORT`.

### OpenAI API key

Без ключа приложение работает в demo/fallback режиме: расчётное ядро полностью доступно.

Для настоящего AI-agent режима добавьте в Render Environment Variables:

```text
OPENAI_API_KEY=ваш_ключ
OPENAI_MODEL=gpt-5-mini
```

**Не** добавляйте ключ в GitHub, `.env`, HTML или JavaScript.

## Онлайн-деплой — Railway

В проекте также есть `railway.json`.

1. Создайте обычный GitHub repository и загрузите проект.
2. В Railway выберите **New Project → Deploy from GitHub repo**.
3. Выберите repository.
4. Railway определит Python/FastAPI и запустит сервис.
5. Сгенерируйте public domain в Networking.
6. При необходимости добавьте `OPENAI_API_KEY` и `OPENAI_MODEL` в Variables.

## Docker

Проект содержит `Dockerfile` и может быть запущен так:

```bash
docker build -t stockpilot-ai .
docker run -p 8000:8000 stockpilot-ai
```

## Демо-данные

`data/demo.json` — демонстрационная выборка. Реальные клиентские данные не должны попадать в публичный репозиторий.

## Тесты

```bash
pytest -q
```

Тесты покрывают ключевые инварианты: влияние товара в пути, сезонность, MOQ, stockout uplift и объяснимость.

## HackAlem deployment checklist

- [x] Работает как веб-приложение.
- [x] Не требует Codespaces.
- [x] Cloud-ready FastAPI.
- [x] Public health endpoint.
- [x] Render config.
- [x] Railway config.
- [x] Dockerfile.
- [x] AI optional: demo работает без API key.
- [x] Secrets не хранятся в репозитории.
- [x] Автоматическая отправка поставщику отсутствует.
- [x] Клиентские идентификаторы не используются в расчёте.
