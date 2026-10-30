"""HTTP client: proxy resolution chain (explicit > system > auto VPN scan)."""
import pytest

from services import http_client as hc


@pytest.mark.asyncio
async def test_ensure_local_proxy_prefers_explicit(monkeypatch):
    c = hc.HttpClient(proxy="http://127.0.0.1:9999")
    assert await c.ensure_local_proxy() == "http://127.0.0.1:9999"


@pytest.mark.asyncio
async def test_ensure_local_proxy_uses_system(monkeypatch):
    monkeypatch.setattr(hc, "detect_proxy", lambda: "http://sys:8080")
    c = hc.HttpClient()
    assert await c.ensure_local_proxy() == "http://sys:8080"


@pytest.mark.asyncio
async def test_ensure_local_proxy_autodetects_vpn_port(monkeypatch):
    monkeypatch.setattr(hc, "detect_proxy", lambda: None)
    async def fake_scan():
        return ["http://127.0.0.1:7890", "http://127.0.0.1:10808"]
    monkeypatch.setattr(hc, "find_local_proxy_ports_async", fake_scan)
    c = hc.HttpClient()
    assert await c.ensure_local_proxy() == "http://127.0.0.1:7890"
    # результат кешируется: повторный вызов не пересканирует (fake_scan ломается при 2-м вызове)
    async def boom():
        raise AssertionError("scan must not run twice")
    monkeypatch.setattr(hc, "find_local_proxy_ports_async", boom)
    assert await c.ensure_local_proxy() == "http://127.0.0.1:7890"


@pytest.mark.asyncio
async def test_no_proxy_anywhere(monkeypatch):
    monkeypatch.setattr(hc, "detect_proxy", lambda: None)
    async def none_scan():
        return []
    monkeypatch.setattr(hc, "find_local_proxy_ports_async", none_scan)
    c = hc.HttpClient()
    assert await c.ensure_local_proxy() is None
