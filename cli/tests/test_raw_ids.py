from __future__ import annotations

from inspire.cli.utils.raw_ids import scrub_raw_ids


def test_scrub_raw_ids_keeps_human_path_segments_with_model_word() -> None:
    path = (
        "/inspire/hdd/project/embodied-multimodality/tongjingqi-CZXS25110029/"
        "codex-smoke-model-20260509"
    )

    assert scrub_raw_ids(path) == path


def test_scrub_raw_ids_keeps_date_suffixed_names() -> None:
    text = "job-smoke-20260507 notebook-smoke-20260507 model-smoke-20260509"

    assert scrub_raw_ids(text) == text


def test_scrub_raw_ids_scrubs_compact_platform_handles() -> None:
    text = "rj-abc ray-abc-1 hpc-job-123 img-001 image-abc-def"

    assert (
        scrub_raw_ids(text)
        == "<redacted> <redacted> <redacted> <redacted> <redacted>"
    )


def test_scrub_raw_ids_scrubs_platform_prefixed_ids() -> None:
    text = (
        "model-ca9ed4f5-9533-4241-9c59-984831007296 "
        "image-ca9ed4f5-9533-4241-9c59-984831007296 "
        "sv-ca9ed4f5-9533-4241-9c59-984831007296"
    )

    assert scrub_raw_ids(text) == "<redacted> <redacted> <redacted>"


def test_scrub_raw_ids_scrubs_ray_instance_handles() -> None:
    text = "rj-abc-vhd4h-head-qlrtm rj-abc-vhd4h-w-worker-ttrv4"

    assert scrub_raw_ids(text) == "<redacted> <redacted>"


def test_scrub_raw_ids_scrubs_compute_group_and_workspace_handles() -> None:
    text = (
        "cg-abcdef12 group-123456 compute-group-abc123 "
        "workspace-abcdef proj-123456"
    )

    assert scrub_raw_ids(text) == "<redacted> <redacted> <redacted> <redacted> <redacted>"


def test_scrub_raw_ids_scrubs_short_numeric_platform_handles() -> None:
    text = "lcg-1 cg-1 ws-1 (lcg-12, cg-123; ws-456)"

    assert scrub_raw_ids(text) == (
        "<redacted> <redacted> <redacted> "
        "(<redacted>, <redacted>; <redacted>)"
    )


def test_scrub_raw_ids_keeps_name_like_prefixed_values() -> None:
    text = "group-a cg-alpha workspace-1-name"

    assert scrub_raw_ids(text) == text
