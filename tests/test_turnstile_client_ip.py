"""Turnstile visitor-IP forwarding trust-boundary tests."""

import pytest
from starlette.requests import Request

from server.routes import turnstile


WORKER_IP = "2a06:98c0:3600::103"
VISITOR_IP = "203.0.113.42"


def _request(headers: dict[str, str], direct_ip: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/turnstile/verify",
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in headers.items()
            ],
            "client": (direct_ip, 12345),
        }
    )


def test_authenticated_worker_forwarding_preserves_visitor_ip(monkeypatch):
    monkeypatch.setattr(turnstile.config, "SSR_AUTH_SECRET", "shared-secret")
    request = _request(
        {
            "CF-Connecting-IP": WORKER_IP,
            "X-Forwarded-Client-IP": VISITOR_IP,
            "X-SSR-Auth": "shared-secret",
        }
    )

    assert turnstile._resolve_client_ip(request) == VISITOR_IP


def test_spoofed_forwarded_ip_is_ignored(monkeypatch):
    monkeypatch.setattr(turnstile.config, "SSR_AUTH_SECRET", "shared-secret")
    request = _request(
        {
            "CF-Connecting-IP": WORKER_IP,
            "X-Forwarded-Client-IP": VISITOR_IP,
            "X-SSR-Auth": "wrong-secret",
        }
    )

    assert turnstile._resolve_client_ip(request) == WORKER_IP


def test_unauthenticated_forwarded_ip_is_ignored(monkeypatch):
    monkeypatch.setattr(turnstile.config, "SSR_AUTH_SECRET", "shared-secret")
    request = _request(
        {
            "CF-Connecting-IP": WORKER_IP,
            "X-Forwarded-Client-IP": VISITOR_IP,
        }
    )

    assert turnstile._resolve_client_ip(request) == WORKER_IP


def test_malformed_forwarded_ip_is_ignored(monkeypatch):
    monkeypatch.setattr(turnstile.config, "SSR_AUTH_SECRET", "shared-secret")
    request = _request(
        {
            "CF-Connecting-IP": WORKER_IP,
            "X-Forwarded-Client-IP": "not-an-ip",
            "X-SSR-Auth": "shared-secret",
        }
    )

    assert turnstile._resolve_client_ip(request) == WORKER_IP


def test_direct_cloudflare_request_uses_visitor_ip(monkeypatch):
    monkeypatch.setattr(turnstile.config, "SSR_AUTH_SECRET", "shared-secret")
    request = _request({"CF-Connecting-IP": VISITOR_IP})

    assert turnstile._resolve_client_ip(request) == VISITOR_IP


@pytest.mark.asyncio
async def test_verification_submits_authenticated_visitor_ip(monkeypatch):
    monkeypatch.setattr(turnstile.config, "SSR_AUTH_SECRET", "shared-secret")
    submitted: dict[str, str | None] = {}

    async def verify(token: str, remote_ip: str | None):
        submitted.update(token=token, remote_ip=remote_ip)
        return {"success": True}

    monkeypatch.setattr(turnstile, "verify_turnstile_token", verify)
    monkeypatch.setattr(turnstile, "sign_session_token", lambda timestamp, ip_hash="": "session")
    request = _request(
        {
            "CF-Connecting-IP": WORKER_IP,
            "X-Forwarded-Client-IP": VISITOR_IP,
            "X-SSR-Auth": "shared-secret",
        }
    )

    response = await turnstile.verify_turnstile(
        turnstile.TurnstileVerifyRequest(token="challenge-token"), request
    )

    assert response == {"success": True, "session_token": "session"}
    assert submitted == {
        "token": "challenge-token",
        "remote_ip": VISITOR_IP,
    }
