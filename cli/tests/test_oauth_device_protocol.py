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
    assert http.post.call_args_list[0].kwargs["json"] == {"scope": "openid profile email"}


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
