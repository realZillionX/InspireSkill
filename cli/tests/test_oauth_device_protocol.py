from unittest.mock import Mock

import pytest

from inspire.platform.web.session.models import AuthenticationError
from inspire.platform.web.session.oauth import DeviceAuthorization, platform_headers


def test_platform_headers_are_origin_scoped():
    headers = platform_headers("https://platform.test", "https://platform.test/api", "secret")
    assert headers["Authorization"] == "Bearer secret"
    assert headers["X-Inspire-Client-Source"].startswith("inspire-cli/inspireskill-")
    for target in ("https://notebook.test", "http://platform.test", "https://platform.test:444"):
        assert platform_headers("https://platform.test", target, "secret") == {}


def response(status, body):
    return Mock(status_code=status, json=Mock(return_value=body))


@pytest.mark.parametrize("terminal", ["success", "expired_token", "access_denied", "invalid_grant"])
def test_device_polling(terminal):
    now = [0.0]
    sleeps = []
    notifications = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    tokens = {"access_token": "access-secret", "refresh_token": "refresh-secret",
              "expires_in": 3600, "refresh_expires_in": 6000}
    http = Mock()
    http.post.side_effect = [
        response(200, {"code": 0, "data": {"device_code": "device-secret", "user_code": "ABCD",
                 "verification_uri_complete": "https://identity.test/device?user_code=ABCD",
                 "expires_in": 600, "interval": 5}}),
        response(400, {"message": "authorization_pending"}),
        response(400, {"message": "slow_down"}),
        response(200, {"code": 0, "data": tokens}) if terminal == "success"
        else response(400, {"message": terminal}),
    ]
    flow = DeviceAuthorization("https://platform.test", http, clock=lambda: now[0], sleep=sleep)

    def notify(*args):
        assert http.post.call_count == 1
        notifications.append(args)

    if terminal == "success":
        result = flow.login(notify)
        assert result.refresh_token == "refresh-secret"
        assert "secret" not in repr(result)
    else:
        with pytest.raises(AuthenticationError, match=terminal):
            flow.login(notify)
    assert sleeps == [5, 5, 10]
    assert len(notifications) == 1
    for call in http.post.call_args_list:
        assert call.kwargs["allow_redirects"] is False
    assert http.post.call_args_list[0].kwargs["json"] == {"scope": "openid profile email offline_access"}


def test_refresh_returns_rotated_token():
    http = Mock()
    http.post.return_value = response(200, {"code": 0, "data": {
        "access_token": "new-access", "refresh_token": "new-refresh",
        "expires_in": 3600, "refresh_expires_in": 6000,
    }})
    result = DeviceAuthorization("https://platform.test", http).refresh("old-refresh")
    assert result.refresh_token == "new-refresh"
    assert http.post.call_args.kwargs["json"] == {"refresh_token": "old-refresh"}


@pytest.mark.parametrize("budget,slow_down", [(1, False), (8, True), (60, True)])
@pytest.mark.parametrize("transport_budget", [False, True])
def test_device_deadline_clips_poll_sleeps(budget, slow_down, transport_budget):
    from inspire.platform.web.runtime import active_transport

    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    http = Mock()
    device = response(200, {"code": 0, "data": {
        "device_code": "private", "user_code": "code", "verification_uri": "https://test.invalid",
        "expires_in": 600, "interval": 5,
    }})
    pending = response(400, {"message": "slow_down" if slow_down else "authorization_pending"})
    http.post.side_effect = lambda *a, **kw: device if http.post.call_count == 1 else pending
    owner = Mock()
    owner.remaining.side_effect = lambda: budget - now[0]
    token = active_transport.set(owner if transport_budget else None)
    try:
        flow = DeviceAuthorization("https://test.invalid", http, clock=lambda: now[0], sleep=sleep)
        with pytest.raises(AuthenticationError, match="expired"):
            flow.login(lambda *a: None, deadline=None if transport_budget else budget)
    finally:
        active_transport.reset(token)
    assert sum(sleeps) == budget
    assert now[0] == budget
    if budget == 1:
        assert sleeps == [1]
        assert http.post.call_count == 1
    for call in http.post.call_args_list:
        assert call.kwargs["timeout"] <= budget


@pytest.mark.parametrize("message,reason", [
    ("invalid_grant", "invalid_grant"),
    ("credential SECRET-REFRESH rejected", "refresh_rejected"),
    ("invalid_grant\n", "refresh_rejected"),
    ("x" * 65, "refresh_rejected"),
    (None, "refresh_rejected"),
    ({"error": "invalid_grant"}, "refresh_rejected"),
])
def test_refresh_rejection_sanitized(message, reason):
    from inspire.platform.web.session.models import RefreshRejectedError

    http = Mock()
    http.post.return_value = response(400, {"code": -100000, "message": message})
    with pytest.raises(RefreshRejectedError) as caught:
        DeviceAuthorization("https://platform.test", http).refresh("SECRET-REFRESH")
    assert isinstance(caught.value, AuthenticationError)
    assert caught.value.reason == reason
    assert caught.value.status == 400
    assert "HTTP 400" in str(caught.value)
    assert "SECRET-REFRESH" not in str(caught.value)


def test_refresh_network_error_propagates():
    import requests

    http = Mock()
    error = requests.ConnectionError("unavailable")
    http.post.side_effect = error
    with pytest.raises(requests.ConnectionError) as caught:
        DeviceAuthorization("https://platform.test", http).refresh("fake-refresh")
    assert caught.value is error


@pytest.mark.parametrize("lifetime,typ,offline", [
    (0, None, True), (None, None, True), (604800, None, False),
    (604800, "Offline", True), (604800, "Refresh", False),
])
def test_token_refresh_classification(monkeypatch, lifetime, typ, offline):
    import base64
    import json
    import math
    from inspire.platform.web.session import oauth

    monkeypatch.setattr(oauth.time, "time", lambda: 1000)
    refresh = "malformed"
    if typ:
        payload = base64.urlsafe_b64encode(json.dumps({"typ": typ}).encode()).decode().rstrip("=")
        refresh = f"header.{payload}.signature"
    data = {"access_token": "fake-access", "refresh_token": refresh, "expires_in": 3600}
    if lifetime is not None:
        data["refresh_expires_in"] = lifetime
    result = DeviceAuthorization._tokens({"data": data})
    assert result.refresh_offline is offline
    assert math.isfinite(result.refresh_expires_at)
    assert result.refresh_expires_at == 1000 + (30 * 86400 if offline else 604800)
    assert result.access_expires_at == 4600


@pytest.mark.parametrize("refresh", ["not-a-jwt", "a.!.b", "a.e30.b", "a.W10.b", "a._w.b"])
@pytest.mark.parametrize("lifetime", [0, 604800])
def test_malformed_refresh_claims_do_not_break_classification(refresh, lifetime):
    tokens = DeviceAuthorization._tokens({"data": {
        "access_token": "fake-access", "refresh_token": refresh,
        "expires_in": 3600, "refresh_expires_in": lifetime,
    }})
    assert tokens.refresh_offline is (lifetime == 0)
