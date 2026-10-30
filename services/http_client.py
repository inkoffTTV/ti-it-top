"""Единый HTTP-клиент для внешних провайдеров.

Проблема, которую это решает: Cloudflare на dotabuff.com / api.stratz.com
блокирует запросы по отпечатку TLS (JA3). У aiohttp он «питоновский», и CF
отдаёт 403 даже при корректных заголовках — тогда как обычный браузер с того
же IP проходит. Поэтому здесь реализована цепочка транспортов:

1. curl_cffi (если установлен) — эмулирует реальный TLS-fingerprint Chrome;
2. aiohttp (всегда доступен, входит в requirements) — запасной транспорт;
3. прямой HTTPS-транспорт через ssl+threadpool — на случай, если aiohttp
   не может подключиться (TLS handshake error и т.п.);
4. все запросы идут через системный proxy-detector (Windows Registry /
   env HTTP(S)_PROXY), поэтому локальный VPN/прокси подхватывается автоматически.

Можно явно задать свой прокси (например, локальный порт VPN):
    HTTP_PROXY_URL=http://127.0.0.1:7890   # .env
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import shutil
import socket
import ssl
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("HTTP")

try:  # optional dependency — основной обход Cloudflare-фильтров
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession

    HAS_CURL_CFFI = True
except Exception:  # noqa: BLE001 — отсутствие либы не должно ломать бота
    _CurlAsyncSession = None  # type: ignore[assignment]
    HAS_CURL_CFFI = False

DEFAULT_TIMEOUT = 8.0

# Отпечатки, проверенные против Dotabuff/Cloudflare с сервера (29.09.2026: HTTP 200).
_IMPERSONATE_FALLBACKS = ("chrome131", "chrome", "chrome124", "safari17_0", "firefox133")
_impersonate_warned: set[str] = set()


def _supported_impersonate_targets() -> set[str] | None:
    """Список отпечатков установленной curl_cffi; None — проверить нельзя."""
    try:
        from curl_cffi.requests import impersonate as _imp

        values = {b.value for b in _imp.BrowserType}
        values.update(getattr(_imp, "REAL_TARGET_MAP", {}) or {})
        values.update({"chrome", "safari", "firefox", "edge"})
        return values
    except Exception:  # noqa: BLE001 — старая/новая версия без этого API
        return None


def resolve_impersonate(target: str | None) -> str:
    """Возвращает поддерживаемый curl_cffi TLS-отпечаток.

    Причина: HTTP_IMPERSONATE=chrome126 отсутствует в curl_cffi 0.16.x. curl_cffi
    бросал ошибку, HttpClient молча уходил на aiohttp, а Cloudflare Dotabuff
    отвечал 403 по «питоновскому» TLS-отпечатку. .env не меняем — подменяем здесь.
    """
    wanted = (target or "").strip() or _IMPERSONATE_FALLBACKS[0]
    supported = _supported_impersonate_targets()
    if supported is None or wanted in supported:
        return wanted
    for candidate in _IMPERSONATE_FALLBACKS:
        if candidate in supported:
            if wanted not in _impersonate_warned:
                _impersonate_warned.add(wanted)
                logger.warning("[HTTP] curl_cffi не поддерживает отпечаток %r — используется %r", wanted, candidate)
            return candidate
    return wanted

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/json,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


def detect_proxy() -> str | None:
    """Возвращает прокси-URL: явный из env, иначе системный (Windows registry)."""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var)
        if val:
            return val if "://" in val else f"http://{val}"
    # Windows: реестр Internet Settings (там же берёт адрес браузер).
    try:
        import winreg  # type: ignore

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        with key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enable:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                if server and "=" not in server:
                    return f"http://{server}"
    except Exception:  # noqa: BLE001 — не Windows или ключ не найден
        pass
    return None


@dataclass(slots=True)
class HttpResponse:
    status: int
    body: str
    headers: dict[str, str]

    async def text(self) -> str:
        return self.body

    async def json(self) -> Any:
        return _json.loads(self.body)


class HttpClient:
    """Асинхронный клиент с цепочкой транспортов и системным прокси."""

    def __init__(
        self,
        proxy: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        impersonate: str = "chrome124",
    ):
        self.timeout = timeout
        self.impersonate = resolve_impersonate(impersonate) if HAS_CURL_CFFI else impersonate
        self._proxy_explicit = proxy
        self._proxy_detected: str | None | bool = False  # lazy
        self._local_proxies: list[str] | None = None
        self._curl_session = None
        self._curl_session_proxy: str | None = None

    def _curl_kwargs(self) -> dict[str, Any]:
        """impersonate включает реальный TLS/H2/HTTP3-fingerprint Chrome —
        без него curl_cffi ведёт себя как обычный Python-клиент и Cloudflare
        блокирует так же (403)."""
        return {"impersonate": self.impersonate, "timeout": self.timeout}

    # ------------------------------------------------------------ properties

    @property
    def proxy(self) -> str | None:
        if self._proxy_detected is False:
            self._proxy_detected = detect_proxy()
            if self._proxy_detected:
                logger.info("[HTTP] Detected system proxy: %s", self._proxy_detected)
        return self._proxy_explicit or self._proxy_detected  # type: ignore[return-value]

    async def ensure_local_proxy(self) -> str | None:
        """Прокси для запросов: явный > системный > авто-поиск локального VPN.

        Если в .env нет HTTP_PROXY_URL и система не сообщает прокси, бот сканирует
        localhost на порты популярных VPN-клиентов (MantaRay/v2rayN/Clash/Mihomo:
        7890, 10808, 10809, 1080 и др.) и использует первый найденный. Это позволяет
        ходить к Dotabuff/STRATZ через тот же туннель, через который у пользователя
        открывается браузер. Результат кешируется на весь срок жизни клиента.
        """
        if self._proxy_explicit:
            return self._proxy_explicit
        sysp = self.proxy
        if sysp:
            return sysp
        if self._local_proxies is None:
            try:
                self._local_proxies = await find_local_proxy_ports_async()
            except Exception as e:  # noqa: BLE001 — скан не критичен
                logger.debug("[HTTP] local proxy scan error: %s", e)
                self._local_proxies = []
            if self._local_proxies:
                logger.info("[HTTP] Auto-detected local VPN proxy: %s", self._local_proxies[0])
        return self._local_proxies[0] if self._local_proxies else None

    async def _proxy_candidates(self) -> list[str]:
        """Все известные прокси-кандидаты: явный > системный > авто-скан VPN."""
        out: list[str] = []
        if self._proxy_explicit:
            out.append(self._proxy_explicit)
        sysp = self.proxy
        if sysp and sysp not in out:
            out.append(sysp)
        await self.ensure_local_proxy()  # fills _local_proxies
        for x in self._local_proxies or []:
            if x not in out:
                out.append(x)
        return out

    # ------------------------------------------------------------ transports

    async def _curl_get(self, url: str, headers: dict[str, str], force_proxy: str | None | bool = False) -> HttpResponse | None:
        """curl_cffi: TLS-fingerprint браузера + proxy (env или явный)."""
        if not HAS_CURL_CFFI:
            return None
        try:
            prx = force_proxy if force_proxy is not False else await self.ensure_local_proxy()
            # Сессия создаётся/пересоздаётся при смене прокси (VPN мог подняться позже старта бота)
            if self._curl_session is None or self._curl_session_proxy != prx:
                old_sess = self._curl_session
                sess_kwargs = dict(self._curl_kwargs())
                if prx:
                    sess_kwargs["proxy"] = prx
                self._curl_session = _CurlAsyncSession(**sess_kwargs)
                self._curl_session_proxy = prx
                if old_sess is not None:
                    try:
                        await old_sess.close()
                    except Exception:  # noqa: BLE001
                        pass
            kwargs: dict[str, Any] = {
                "headers": headers,
                "timeout": self.timeout,
                "impersonate": self.impersonate,
            }
            if prx:
                kwargs["proxy"] = prx
            resp = await self._curl_session.get(url, **kwargs)
            return HttpResponse(
                status=resp.status_code,
                body=resp.text or "",
                headers={k.lower(): v for k, v in dict(resp.headers).items()},
            )
        except Exception as e:  # noqa: BLE001 — пробуем следующий транспорт
            logger.debug("[HTTP] curl_cffi GET failed: %s", e)
            return None

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        hdrs = {**BROWSER_HEADERS, **(headers or {})}
        # 1) curl_cffi (лучший шанс пройти Cloudflare); при неудаче пробуем
        # все найденные локальные VPN-прокси по очереди.
        proxies = await self._proxy_candidates()
        last_r = None
        for px in ([None] + proxies):
            r = await self._curl_get(url, hdrs, force_proxy=px)
            if r is not None and r.status < 400:
                return r
            if r is not None:
                last_r = r
            if len(proxies) <= 1:
                break
        if last_r is not None:
            return last_r
        # 2) aiohttp
        try:
            import aiohttp

            to = aiohttp.ClientTimeout(total=timeout or self.timeout)
            async with aiohttp.ClientSession(timeout=to) as s:
                async with s.get(url, headers=hdrs, proxy=await self.ensure_local_proxy()) as resp:
                    body = await resp.text(errors="replace")
                    return HttpResponse(
                        status=resp.status,
                        body=body,
                        headers={k.lower(): v for k, v in resp.headers.items()},
                    )
        except Exception as e:  # noqa: BLE001
            logger.debug("[HTTP] aiohttp GET failed: %s", e)
        # 3) urllib в threadpool (со своим TLS и env-proxy)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._urllib_get, url, hdrs)

    def _urllib_get(self, url: str, headers: dict[str, str]) -> HttpResponse:
        req = urllib.request.Request(url, headers=headers)
        ctx = ssl.create_default_context()
        opener_args: list[Any] = []
        _px = self._proxy_explicit or detect_proxy()
        if _px:
            opener_args.append(
                urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": _px, "https": _px})
                )
            )
        try:
            if opener_args:
                opener = opener_args[0]
                with opener.open(req, timeout=self.timeout) as resp:
                    return HttpResponse(
                        status=resp.status,
                        body=resp.read().decode("utf-8", errors="replace"),
                        headers={k.lower(): v for k, v in resp.headers.items()},
                    )
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:  # noqa: S310
                return HttpResponse(
                    status=resp.status,
                    body=resp.read().decode("utf-8", errors="replace"),
                    headers={k.lower(): v for k, v in resp.headers.items()},
                )
        except Exception as e:  # noqa: BLE001
            # urllib raises HTTPError for 4xx/5xx — сохраняем статус и тело.
            http_err = getattr(e, "code", None)
            if http_err is not None:
                try:
                    body = e.read().decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    body = ""
                hdrs = {}
                try:
                    hdrs = {k.lower(): v for k, v in e.headers.items()}  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
                return HttpResponse(status=int(http_err), body=body, headers=hdrs)
            raise ConnectionError(f"all HTTP transports failed for {url}: {e}") from e

    # ------------------------------------------------------------ POST (GraphQL)

    async def post_json(
        self,
        url: str,
        payload: dict,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        hdrs = {**BROWSER_HEADERS, "Content-Type": "application/json", **(headers or {})}
        # 1) curl_cffi (перебор прокси-кандидатов при неудаче)
        if HAS_CURL_CFFI:
            proxies = await self._proxy_candidates()
            last_exc: Exception | None = None
            for px in ([None] + proxies):
                try:
                    r = await self._curl_post(url, payload, hdrs, timeout, force_proxy=px)
                    if r is not None and r.status < 400:
                        return r
                    if r is not None:
                        return r  # CF вернул 4xx/5xx через все — отдаём как есть (провайдер сам решит)
                except Exception as e:  # noqa: BLE001
                    last_exc = e
                    if len(proxies) <= 1:
                        break
            if last_exc is not None:
                logger.debug("[HTTP] curl_cffi POST failed: %s", last_exc)
        # 2) aiohttp
        try:
            import aiohttp

            to = aiohttp.ClientTimeout(total=timeout or self.timeout)
            async with aiohttp.ClientSession(timeout=to) as s:
                async with s.post(url, json=payload, headers=hdrs, proxy=await self.ensure_local_proxy()) as resp:
                    body = await resp.text(errors="replace")
                    return HttpResponse(
                        status=resp.status,
                        body=body,
                        headers={k.lower(): v for k, v in resp.headers.items()},
                    )
        except Exception as e:  # noqa: BLE001
            logger.debug("[HTTP] aiohttp POST failed: %s", e)
        # 3) urllib
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._urllib_post, url, payload, hdrs)

    def _urllib_post(self, url: str, payload: dict, headers: dict[str, str]) -> HttpResponse:
        data = _json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return HttpResponse(
                    status=resp.status,
                    body=resp.read().decode("utf-8", errors="replace"),
                    headers={k.lower(): v for k, v in resp.headers.items()},
                )
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            if code is not None:
                try:
                    body = e.read().decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    body = ""
                return HttpResponse(status=int(code), body=body, headers={})
            raise ConnectionError(f"all HTTP transports failed for {url}: {e}") from e

    async def _curl_post(
        self, url: str, payload: dict, headers: dict[str, str],
        timeout: float | None, force_proxy: str | None | bool = False,
    ) -> HttpResponse | None:
        """curl_cffi POST (GraphQL) с браузерным TLS-отпечатком и прокси."""
        if not HAS_CURL_CFFI:
            return None
        try:
            prx = force_proxy if force_proxy is not False else await self.ensure_local_proxy()
            if self._curl_session is None or self._curl_session_proxy != prx:
                sess_kwargs = dict(self._curl_kwargs())
                if prx:
                    sess_kwargs["proxy"] = prx
                self._curl_session = _CurlAsyncSession(**sess_kwargs)
                self._curl_session_proxy = prx
            kwargs: dict[str, Any] = {
                "headers": headers,
                "data": _json.dumps(payload),
                "timeout": timeout or self.timeout,
                "impersonate": self.impersonate,
            }
            if prx:
                kwargs["proxy"] = prx
            resp = await self._curl_session.post(url, **kwargs)
            return HttpResponse(
                status=resp.status_code,
                body=resp.text or "",
                headers={k.lower(): v for k, v in dict(resp.headers).items()},
            )
        except Exception as e:  # noqa: BLE001 — пробуем следующий транспорт/прокси
            logger.debug("[HTTP] curl_cffi POST failed (%s): %s", prx, e)
            raise

    # ------------------------------------------------------------ CLI fallback

    async def curl_cli_get(self, url: str, headers: dict[str, str] | None = None) -> HttpResponse:
        """Крайняя мера: системный curl.exe (Windows 10+ умеет --proxy и TLS1.3)."""
        curl = shutil.which("curl") or shutil.which("curl.exe")
        if not curl:
            raise ConnectionError("curl binary not found")
        cmd = [curl, "-sS", "-L", "--max-time", str(int(self.timeout)),
               "-w", "\n%{http_code}", url]
        for k, v in {**BROWSER_HEADERS, **(headers or {})}.items():
            cmd += ["-H", f"{k}: {v}"]
        if self.proxy:
            cmd += ["--proxy", self.proxy]
        loop = asyncio.get_running_loop()

        def _run() -> HttpResponse:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout + 5)  # noqa: S603
            body, _, code = out.stdout.rpartition("\n")
            try:
                status = int(code.strip())
            except ValueError:
                status = 0
            return HttpResponse(status=status, body=body, headers={})

        return await loop.run_in_executor(None, _run)

    async def close(self) -> None:
        if self._curl_session is not None:
            try:
                await self._curl_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._curl_session = None


# ---------------------------------------------------------------------------
# module-level shared client (lazy)
_shared: HttpClient | None = None


def get_http_client(proxy: str | None = None, impersonate: str | None = None) -> HttpClient:
    global _shared
    if _shared is None:
        imp = impersonate or os.getenv("HTTP_IMPERSONATE", "chrome126")
        _shared = HttpClient(proxy=proxy, impersonate=imp)
    else:
        if proxy:
            _shared._proxy_explicit = proxy
        resolved = resolve_impersonate(impersonate) if (impersonate and HAS_CURL_CFFI) else impersonate
        if resolved and resolved != _shared.impersonate:
            # конфиг изменился (перезапуск/обновление) — пересоздаём сессию
            _shared.impersonate = resolved
            _shared._curl_session = None
    return _shared


async def close_shared_client() -> None:
    global _shared
    if _shared is not None:
        await _shared.close()
        _shared = None


def has_tls_stack() -> bool:
    """sanity: ssl доступен (используется в smoke-тестах)."""
    return hasattr(ssl, "create_default_context") and hasattr(socket, "AF_INET")


async def find_local_proxy_ports_async() -> list[str]:
    """Сканирует localhost на порты популярных VPN/прокси-клиентов и проверяет,
    что прокси реально умеет CONNECT к dotabuff.com (иначе Cloudflare-трафик
    через него не пойдёт). MantaRay / v2rayN / Clash / Mihomo / Xray обычно
    слушают локальный HTTP-портmixed — бот сам его находит и использует, если
    пользователь не задал HTTP_PROXY_URL вручную.

    Возвращает список рабочих http-прокси в порядке приоритета.
    """
    candidates = [
        7890, 7891, 7892, 7893,   # Clash / Mihomo (mixed)
        10808, 10809,             # v2rayN default http/socks
        1080, 1081,               # generic SOCKS
        8888, 8118, 3128,         # common http proxies
        20171, 20172,             # some clients
    ]

    async def _probe(port: int) -> bool:
        """TCP connect + минимальный HTTP CONNECT handshake к dotabuff:443."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.3
            )
        except Exception:  # noqa: BLE001 — closed/refused — обычный случай
            return False
        try:
            writer.write(b"CONNECT www.dotabuff.com:443 HTTP/1.1\r\nHost: www.dotabuff.com:443\r\n\r\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=0.6)
            # ожидаем "HTTP/1.x 200 ..." — значит прокси живой и туннелит TLS
            return b"200" in line[:20]
        except Exception:  # noqa: BLE001
            return False
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    results = await asyncio.gather(*(_probe(p_) for p_ in candidates))
    found = [f"http://127.0.0.1:{port}" for port, ok in zip(candidates, results) if ok]
    if found:
        logger.info("[HTTP] Local VPN proxy candidates (CONNECT-tested): %s", ", ".join(found))
    return found


    results = await asyncio.gather(*(_probe(p_) for p_ in candidates))
    return [f"http://127.0.0.1:{port}" for port, ok in zip(candidates, results) if ok]
