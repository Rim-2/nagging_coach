"""telegram_client.send_message 의 중복 방지 분류 단위 테스트.

요청은 도달했는데 응답만 늦은 경우(read timeout)를 '실패'로 보고 재시도하면
텔레그램엔 멱등키가 없어 같은 메시지가 두 번 간다. send_message 가 모호한
경우(True=재시도 금지)와 확실한 미도달(False=재시도 허용)을 구분하는지 검증.
"""

from __future__ import annotations

import requests

import telegram_client


def _client():
    return telegram_client.TelegramClient("dummy-token")  # 네트워크 호출 없음


def test_send_ok(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_call", lambda *a, **k: {"message_id": 1})
    assert c.send_message(1, "hi") is True


def test_read_timeout_treated_as_sent(monkeypatch):
    # 도달했을 수 있음 → 재시도 막으려고 True (중복 방지)
    c = _client()

    def boom(*a, **k):
        raise requests.exceptions.ReadTimeout("slow response")

    monkeypatch.setattr(c, "_call", boom)
    assert c.send_message(1, "hi") is True


def test_connect_timeout_treated_as_failed(monkeypatch):
    # 연결 자체 실패 = 확실히 미도달 → 재시도 안전하게 False
    c = _client()

    def boom(*a, **k):
        raise requests.exceptions.ConnectTimeout("no connection")

    monkeypatch.setattr(c, "_call", boom)
    assert c.send_message(1, "hi") is False


def test_generic_error_treated_as_failed(monkeypatch):
    # 텔레그램이 명시적으로 거부(ok:false 등) → 미도달 → False
    c = _client()

    def boom(*a, **k):
        raise RuntimeError("Telegram API 오류")

    monkeypatch.setattr(c, "_call", boom)
    assert c.send_message(1, "hi") is False
