"""Простой запрос к AI без инструментов (для помощника в боте и проверки игроков).

Использует ту же цепочку провайдеров, что и админский AI (services.ai_assistant):
OpenRouter -> резервная модель -> Groq (если задан AI_BACKUP_API_KEY).
Никаких инструментов и доступа к БД: модель видит только переданный текст.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp

from services.ai_assistant import _ai_providers, _provider_error

logger = logging.getLogger("AI_SIMPLE")


class AIUnavailable(RuntimeError):
    """AI не настроен или все провайдеры сейчас не отвечают."""


async def complete(
    config,
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 700,
    temperature: float = 0.2,
    total_timeout: float = 45.0,
    title: str = "TI:IT TOP",
) -> tuple[str, str]:
    """Возвращает (текст, модель). Бросает AIUnavailable, если ответа нет."""
    providers = _ai_providers(config)
    if not providers:
        raise AIUnavailable("AI не настроен: нет AI_API_KEY в .env сервера.")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout
    errors: list[str] = []
    for provider in providers:
        remaining = deadline - loop.time()
        if remaining < 3:
            break
        url = provider["base_url"].rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        payload = {"model": provider["model"], "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        headers = {
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
            "HTTP-Referer": config.miniapp_url or "https://tiit.top",
            "X-Title": title,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=min(30.0, remaining))
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.post(url, json=payload, headers=headers, proxy=(config.http_proxy_url or None)) as resp:
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:  # noqa: BLE001
                        body = {"error": (await resp.text())[:500]}
                    if resp.status >= 400:
                        raise RuntimeError(_provider_error(resp.status, body))
            choices = (body or {}).get("choices") or []
            text = ((choices[0] if choices else {}).get("message") or {}).get("content")
            if isinstance(text, str) and text.strip():
                return text.strip(), str(body.get("model") or provider["model"])
            raise RuntimeError("пустой ответ модели")
        except Exception as exc:  # noqa: BLE001 — пробуем следующий провайдер
            errors.append(f"{provider['name']}: {str(exc)[:200]}")
            logger.warning("[AI_SIMPLE] %s failed: %s", provider["name"], exc)
    raise AIUnavailable("AI сейчас не отвечает (" + "; ".join(errors)[:600] + ")")


def extract_json(text: str) -> dict[str, Any] | None:
    """Достаёт JSON-объект из ответа модели (модели иногда оборачивают его в ```json)."""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    for candidate in (cleaned, text):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            data = json.loads(candidate[start:end + 1])
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None
