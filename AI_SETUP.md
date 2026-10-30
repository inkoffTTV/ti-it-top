# AI для TI:IT TOP — бесплатная схема с VPN и автоматическим fallback

## Схема, рассчитанная на запуск из РФ

Проект умеет ходить к AI через `HTTP_PROXY_URL`. Если у тебя уже работает локальный HTTP-прокси/VPN-клиент, например на `127.0.0.1:10808`, укажи этот адрес в `.env`. Если порт у VPN другой — укажи его. Сам Mini App и Telegram-бот не меняются.

Рекомендуемая цепочка:

1. **OpenRouter Free Router** — основной провайдер. `openrouter/free` бесплатно маршрутизирует запросы среди доступных free-моделей и умеет отбирать модели с tool calling.
2. **Qwen3.8 27B (free) через OpenRouter** — второй бесплатный шанс на том же API-ключе. Модель поддерживает длинный контекст и агентные задачи.
3. **OpenAI GPT-OSS 120B через Groq** — независимый резервный провайдер, если добавить `GROQ`-подобный API key в `AI_BACKUP_API_KEY`. На Free plan Groq сейчас даёт отдельные дневные/минутные лимиты для GPT-OSS 120B.

### 1. OpenRouter

Открой `https://openrouter.ai/`, создай API key и заполни:

```env
AI_API_KEY=YOUR_OPENROUTER_KEY
AI_BASE_URL=https://openrouter.ai/api/v1/chat/completions
AI_MODEL=openrouter/free
AI_FALLBACK_MODEL=qwen/qwen3.8-27b:free
AI_MAX_TOOL_ROUNDS=5
HTTP_PROXY_URL=http://127.0.0.1:10808
```

`HTTP_PROXY_URL` оставляй пустым только когда внешний AI API и Telegram реально доступны напрямую.

### 2. Независимый запасной AI: Groq

Открой `https://console.groq.com/keys`, создай ключ и добавь:

```env
AI_BACKUP_API_KEY=YOUR_GROQ_KEY
AI_BACKUP_BASE_URL=https://api.groq.com/openai/v1/chat/completions
AI_BACKUP_MODEL=openai/gpt-oss-120b
```

Это независимый путь: если OpenRouter временно недоступен, превышен free-квотный лимит или провайдер вернул ошибку, агент попробует Groq.

### 3. Ещё один вариант — Gemini

Google AI Studio предоставляет API key и бесплатный уровень для части Gemini API, но конкретные лимиты и доступность меняются. Для этого проекта Gemini можно использовать как основной endpoint, потому что Google документирует OpenAI-compatible API и function calling.

```env
AI_API_KEY=YOUR_GEMINI_KEY
AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
AI_MODEL=gemini-3.8-flash
```

## Что выбрать

Для твоего случая я бы оставил OpenRouter Free Router основным, Qwen3.8 27B бесплатным fallback и добавил Groq GPT-OSS 120B как независимый backup. Это не обещает бесконечный бесплатный доступ: все бесплатные планы имеют лимиты и могут менять доступность моделей.

## Безопасность

AI доступна только администраторам. Она не получает прямой доступ к SQL или файловой системе. Сервер разрешает только заранее описанные read-only tools. В prompts не передаются bot token, API keys, Telegram initData, cookies или содержимое `.env`.
