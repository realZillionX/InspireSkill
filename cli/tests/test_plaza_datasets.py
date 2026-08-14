"""数据广场 catalogue: request shapes, parsing, and code resolution."""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.plaza import datasets as plaza_datasets


def _row(code: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "datasetId": 1710,
        "datasetCode": code,
        "projectName": "面向多模态与世界模型的基础架构研究",
        "director": "孙宇涛",
        "maintenance": "孙宇涛",
        "ownerName": "孙宇涛",
        "super": "S3",
        "superSub": 0,
        "state": "active",
        "hasPermission": True,
        "starCount": 0,
        "viewCount": 8,
        "description": "Pixabay-81K",
        "createdAt": "2026-08-13",
        "updatedAt": "2026-08-13",
        "tags": [{"tagId": 47, "tagName": "视频生成", "categoryId": 4}],
    }
    row.update(overrides)
    return row


def _install(monkeypatch: pytest.MonkeyPatch, *payloads: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    queue = list(payloads)

    def fake_plaza_request(method, path, *, params=None, body=None, session=None, timeout=30):  # noqa: ANN001
        calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "body": body,
                "timeout": timeout,
            }
        )
        return queue.pop(0) if queue else {}

    monkeypatch.setattr(plaza_datasets, "plaza_request", fake_plaza_request)
    return calls


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_datasets_sends_paging_and_search(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch, {"list": [_row("pixabay-81k")], "total": 1})

    items, total = plaza_datasets.list_datasets(keyword="  pixabay  ", page=2, page_size=12)

    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/api/datasets/getDatasetsList"
    assert calls[0]["params"] == {"page": 2, "pageSize": 12, "keyword": "pixabay"}
    assert total == 1
    assert [item.code for item in items] == ["pixabay-81k"]


def test_list_datasets_omits_the_tag_filter_when_nothing_is_selected(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch, {"list": [], "total": 531})

    plaza_datasets.list_datasets(tag_ids=[])

    # An empty `tags` value is not a wildcard: the plaza reads it as "matches no
    # tag" and answers with an empty page, so the key must not be sent at all.
    assert "tags" not in calls[0]["params"]


def test_list_datasets_joins_selected_tag_handles(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch, {"list": [], "total": 0})

    plaza_datasets.list_datasets(tag_ids=[47, 26])

    assert calls[0]["params"]["tags"] == "47,26"


def test_list_datasets_parses_a_catalogue_row(monkeypatch) -> None:  # noqa: ANN001
    _install(
        monkeypatch,
        {"list": [_row("pexels-245k", hasPermission=False, state="processing")], "total": 1},
    )

    (item,), _ = plaza_datasets.list_datasets()

    assert item.code == "pexels-245k"
    assert item.project == "面向多模态与世界模型的基础架构研究"
    assert item.grade == "S3"
    assert item.state == "processing"
    assert item.accessible is False
    assert item.tags == ("视频生成",)
    assert item.updated_at == "2026-08-13"


def test_list_datasets_survives_a_malformed_page(monkeypatch) -> None:  # noqa: ANN001
    _install(monkeypatch, {"list": ["not-a-row", {}, _row("videoufo")], "total": "3"})

    items, total = plaza_datasets.list_datasets()

    assert [item.code for item in items] == ["videoufo"]
    assert total == 3


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


def test_get_dataset_detail_posts_the_catalogue_handle(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(
        monkeypatch,
        {
            "datasetId": 1710,
            "datasetCode": "pixabay-81k",
            "licenseName": "CC BY 4.0",
            "licenseUrl": "https://creativecommons.org/licenses/by/4.0/",
            "dataType": "raw",
            "sourceType": "self_import",
            "hasPermission": True,
            "versions": [
                {
                    "versionId": 2310,
                    "versionCode": "v0",
                    "versionState": "active",
                    "filesCount": 81279,
                    "filesSize": 2816752,
                    "dataFormats": '["MP4"]',
                    "updateTime": "2026-08-13 17:59",
                },
                {"versionCode": "", "versionState": "active"},
            ],
        },
    )

    detail = plaza_datasets.get_dataset_detail(1710)

    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/api/datasets/findDatasets"
    assert calls[0]["body"] == {"datasetId": 1710}
    assert detail.code == "pixabay-81k"
    assert detail.license_name == "CC BY 4.0"
    assert [version.code for version in detail.versions] == ["v0"]
    version = detail.versions[0]
    assert version.files_count == 81279
    assert version.files_size_mib == 2816752
    assert version.data_formats == ("MP4",)
    assert version.updated_at == "2026-08-13 17:59"


def test_get_dataset_detail_refuses_an_unresolved_handle(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch, {})

    with pytest.raises(plaza_datasets.UnknownDatasetError):
        plaza_datasets.get_dataset_detail(0)

    assert calls == []


def test_data_formats_fall_back_to_the_raw_value() -> None:
    version = plaza_datasets.DatasetVersion.from_api_response(
        {"versionCode": "v1", "dataFormats": "MP4"}
    )

    assert version.data_formats == ("MP4",)


# ---------------------------------------------------------------------------
# code resolution
# ---------------------------------------------------------------------------


def test_resolve_dataset_by_code_takes_the_exact_code_not_the_first_hit(monkeypatch) -> None:  # noqa: ANN001
    _install(
        monkeypatch,
        {
            "list": [
                _row("pixabay-81k-mirror", datasetId=99),
                _row("pixabay-81k", datasetId=1710),
            ],
            "total": 2,
        },
    )

    match = plaza_datasets.resolve_dataset_by_code("PIXABAY-81K")

    assert match.code == "pixabay-81k"
    assert match.dataset_id == 1710


def test_resolve_dataset_by_code_asks_for_the_rest_when_the_page_hid_the_match(
    monkeypatch,
) -> None:  # noqa: ANN001
    calls = _install(
        monkeypatch,
        {"list": [_row("other-dataset", datasetId=1)], "total": 140},
        {"list": [_row("videoufo", datasetId=1716)], "total": 140},
    )

    match = plaza_datasets.resolve_dataset_by_code("videoufo")

    assert match.dataset_id == 1716
    # The keyword also matches other datasets' descriptions, so one widened
    # request replaces a page walk.
    assert calls[0]["params"]["pageSize"] == 100
    assert calls[1]["params"]["pageSize"] == 140


def test_resolve_dataset_by_code_reports_an_unknown_name(monkeypatch) -> None:  # noqa: ANN001
    _install(monkeypatch, {"list": [], "total": 0})

    with pytest.raises(plaza_datasets.UnknownDatasetError, match="no-such"):
        plaza_datasets.resolve_dataset_by_code("no-such")


def test_resolve_dataset_by_code_requires_a_name(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch, {"list": [], "total": 0})

    with pytest.raises(plaza_datasets.UnknownDatasetError):
        plaza_datasets.resolve_dataset_by_code("   ")

    assert calls == []


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------


def _tag_page() -> dict[str, Any]:
    return {
        "list": [
            {"tagId": 7, "tagName": "文本分类", "categoryId": 1},
            {"tagId": 26, "tagName": "图像生成", "categoryId": 2},
            {"tagId": 47, "tagName": "视频生成", "categoryId": 4},
            {"tagId": 0, "tagName": ""},
        ],
        "total": 4,
    }


def test_list_dataset_tags_labels_the_category(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch, _tag_page())

    tags = plaza_datasets.list_dataset_tags()

    assert calls[0]["path"] == "/api/datasetTags/getDatasetTagsList"
    assert calls[0]["params"] == {"pageSize": 999}
    assert [(tag.name, tag.category) for tag in tags] == [
        ("文本分类", "文本"),
        ("图像生成", "图像"),
        ("视频生成", "视频"),
    ]


def test_resolve_tag_ids_maps_names_and_drops_duplicates(monkeypatch) -> None:  # noqa: ANN001
    _install(monkeypatch, _tag_page())

    assert plaza_datasets.resolve_tag_ids(["视频生成", "图像生成", "视频生成", " "]) == [47, 26]


def test_resolve_tag_ids_does_not_call_out_for_an_empty_selection(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch, _tag_page())

    assert plaza_datasets.resolve_tag_ids([]) == []
    assert calls == []


def test_resolve_tag_ids_reports_unknown_names_with_the_vocabulary(monkeypatch) -> None:  # noqa: ANN001
    _install(monkeypatch, _tag_page())

    with pytest.raises(plaza_datasets.UnknownDatasetTagError) as excinfo:
        plaza_datasets.resolve_tag_ids(["视频生成", "不存在"])

    assert excinfo.value.unknown == ("不存在",)
    assert "视频生成" in excinfo.value.available
