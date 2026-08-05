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


def test_scrub_raw_ids_keeps_name_like_prefixed_values() -> None:
    text = "group-a cg-alpha workspace-1-name"

    assert scrub_raw_ids(text) == text


def test_scrub_raw_ids_keeps_everyday_words_the_platform_never_mints() -> None:
    """``node``/``task``/``pod``/``container`` are names, not handle prefixes.

    Hex digits are also letters, so treating these as handle prefixes redacts
    ordinary log lines — and any resource actually named this way, which a
    name-only CLI then cannot address at all.
    """
    text = (
        "node-001 task-abc pod-123 container-cafe instance-0012 "
        "group-123456 cg-abcdef12 compute-group-abc123 workspace-abcdef "
        "proj-123456 lcg-12"
    )

    assert scrub_raw_ids(text) == text


def test_scrub_raw_ids_keeps_ordinary_log_lines_intact() -> None:
    line = "Epoch 3 | rank 0 on node-001 | step 42 | loss 0.31"

    assert scrub_raw_ids(line) == line


def test_scrub_raw_ids_still_redacts_names_under_a_real_handle_prefix() -> None:
    """Known limitation, unchanged since before the name-only refactor.

    ``job``/``ws``/``model`` are prefixes the platform really does mint, so a
    resource named ``job-1234`` is indistinguishable from a handle by shape
    alone and stays redacted here and rejected at the input boundary. Telling
    the two apart needs the resolver, not a regex.
    """
    assert scrub_raw_ids("job-1234 ws-2024 model-face") == (
        "<redacted> <redacted> <redacted>"
    )


def test_scrub_raw_ids_redacts_a_labelled_uuid_whole() -> None:
    """The labelled-hex rule must not bite the first group off a UUID."""
    text = "wandb: run id 3f2504e0-4f89-11d3-9a0c-0305e82c3301 synced"

    assert scrub_raw_ids(text) == "wandb: run id <redacted> synced"
