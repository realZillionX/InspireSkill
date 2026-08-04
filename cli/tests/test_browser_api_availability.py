from __future__ import annotations

import pytest

from inspire.platform.web.browser_api.availability import api


def test_list_compute_groups_rejects_nonzero_api_code(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "_request_json",
        lambda *_args, **_kwargs: {
            "code": 403,
            "message": "permission denied",
            "data": {},
        },
    )

    with pytest.raises(ValueError, match="permission denied"):
        api.list_compute_groups(
            workspace_id="workspace-one",
            session=object(),  # type: ignore[arg-type]
        )
