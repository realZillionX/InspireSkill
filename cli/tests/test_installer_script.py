from __future__ import annotations

import io
import os
import shutil
import sys
from pathlib import Path
import subprocess
import tarfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1].parent / "scripts"


@pytest.mark.parametrize("installer_name", ["install.sh", "install.ps1"])
def test_installer_post_install_guidance_uses_current_commands(installer_name: str) -> None:
    text = (SCRIPTS / installer_name).read_text(encoding="utf-8")

    assert "inspire login" in text
    assert "inspire login --cas feishu" in text
    assert "inspire account add" not in text
    assert "inspire account check" in text
    assert "inspire config show" not in text
    assert "inspire resources availability --workspace <name> --include-cpu" in text
    assert "inspire resources availability --workspace all" not in text


def test_installer_uses_installed_inspire_for_browser_runtime_setup() -> None:
    installer = Path(__file__).resolve().parents[1].parent / "scripts" / "install.sh"
    text = installer.read_text(encoding="utf-8")

    assert '"$INSPIRE_BIN" _ensure-playwright-runtime' in text


@pytest.mark.skipif(sys.platform == "win32", reason="drives the bash installer end to end")
def test_installer_first_uv_install_without_inspire_on_path(tmp_path: Path) -> None:
    installer = Path(__file__).resolve().parents[1].parent / "scripts" / "install.sh"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    kimi_home = home / "custom-kimi-code"
    kimi_work_root = (
        home
        / "Library"
        / "Application Support"
        / "kimi-desktop"
        / "daimon-share"
        / "daimon"
    )

    (home / ".codex").mkdir()
    (home / ".gemini").mkdir()
    (home / ".cursor").mkdir()
    (home / ".qoderwork").mkdir()
    (home / ".kimi-code").mkdir()
    (home / ".pi" / "agent").mkdir(parents=True)
    kimi_work_root.mkdir(parents=True)
    (bin_dir / "uv").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \"$1 $2\" == \"tool install\" ]]; then\n"
        "  mkdir -p \"$HOME/.local/bin\"\n"
        "  cat >\"$HOME/.local/bin/inspire\" <<'SH'\n"
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == \"--version\" ]]; then echo 'inspire, version test'; exit 0; fi\n"
        "if [[ \"${1:-}\" == \"_ensure-playwright-runtime\" ]]; then exit 0; fi\n"
        "if [[ \"${1:-}\" == \"update\" ]]; then exit 0; fi\n"
        "exit 0\n"
        "SH\n"
        "  chmod +x \"$HOME/.local/bin/inspire\"\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1 $2\" == \"tool update-shell\" ]]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "uv").chmod(0o755)
    (bin_dir / "curl").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bin_dir / "curl").chmod(0o755)
    (bin_dir / "tar").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "out=''\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  if [[ \"$1\" == \"-C\" ]]; then out=\"$2\"; shift 2; else shift; fi\n"
        "done\n"
        "mkdir -p \"$out/InspireSkill-main/references\"\n"
        "printf '# Inspire Skill\\n' > \"$out/InspireSkill-main/SKILL.md\"\n",
        encoding="utf-8",
    )
    (bin_dir / "tar").chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "INSPIRE_SKIP_UPDATE_CHECK": "1",
        "KIMI_CODE_HOME": str(kimi_home),
    }
    result = subprocess.run(
        [
            "bash",
            str(installer),
            "--harness",
            "codex,claude,cursor,opencode,zcode,kimi-code,kimi-work,qoder,qoder-work,antigravity,openclaw,pi,deepseek",
            "--no-schedule",
        ],
        cwd=installer.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "unbound variable" not in result.stderr
    assert (home / ".codex" / "skills" / "inspire" / "SKILL.md").exists()
    codex_metadata = (
        home / ".codex" / "skills" / "inspire" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8")
    assert (
        'short_description: "Operate Inspire with focused references and live platform data."'
        in codex_metadata
    )
    assert (
        'default_prompt: "Use $inspire to plan and execute this Inspire platform task safely."'
        in codex_metadata
    )
    assert (home / ".claude" / "skills" / "inspire" / "SKILL.md").exists()
    assert (home / ".cursor" / "skills" / "inspire" / "SKILL.md").exists()
    assert (
        home / ".config" / "opencode" / "skills" / "inspire" / "SKILL.md"
    ).exists()
    assert (home / ".zcode" / "skills" / "inspire" / "SKILL.md").exists()
    assert (kimi_home / "skills" / "inspire" / "SKILL.md").exists()
    assert (kimi_work_root / "skills" / "inspire" / "SKILL.md").exists()
    assert (home / ".qoder" / "skills" / "inspire" / "SKILL.md").exists()
    assert (home / ".qoderwork" / "skills" / "inspire" / "SKILL.md").exists()
    assert (home / ".gemini" / "config" / "skills" / "inspire" / "SKILL.md").exists()
    assert (home / ".openclaw" / "skills" / "inspire" / "SKILL.md").exists()
    assert (home / ".pi" / "agent" / "skills" / "inspire" / "SKILL.md").exists()
    assert (home / ".dsh" / "skills" / "inspire" / "SKILL.md").exists()
    assert not (home / ".kimi-code" / "skills" / "inspire" / "SKILL.md").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="drives the bash installer end to end")
@pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "auto-detect"])
@pytest.mark.parametrize("directory", ["default", "absolute", "tilde"])
@pytest.mark.parametrize("harness,default_dir,env_key", [
    ("pi", ".pi/agent", "PI_CODING_AGENT_DIR"),
    ("deepseek", ".dsh", "DSH_HOME"),
])
def test_installer_and_uninstall_use_the_harness_directory(
    tmp_path: Path, explicit: bool, directory: str,
    harness: str, default_dir: str, env_key: str,
) -> None:
    home = tmp_path / "home"
    agent_dir = home / (default_dir if directory == "default" else "custom agent home")
    if not explicit:
        agent_dir.mkdir(parents=True)
    shared_skill = home / ".agents" / "skills" / "inspire" / "SKILL.md"
    shared_skill.parent.mkdir(parents=True)
    shared_skill.write_text("shared skill\n", encoding="utf-8")

    bundle = tmp_path / "skill.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for name, content in {
            "InspireSkill-main/SKILL.md": b"pi skill\n",
            "InspireSkill-main/references/setup.md": b"pi reference\n",
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('#!/usr/bin/env bash\ncat "$PI_TEST_BUNDLE"\n', encoding="utf-8")
    curl.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PI_TEST_BUNDLE": str(bundle),
        "INSPIRE_SKIP_UPDATE_CHECK": "1",
    }
    if directory == "absolute":
        env[env_key] = str(agent_dir)
    elif directory == "tilde":
        env[env_key] = "~/custom agent home"
    harness_args = ["--harness", harness] if explicit else []
    installer = SCRIPTS / "install.sh"
    result = subprocess.run(
        ["bash", str(installer), "--no-cli", "--no-schedule", *harness_args],
        env=env, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    target = agent_dir / "skills" / "inspire"
    assert (target / "SKILL.md").read_bytes() == b"pi skill\n"
    assert (target / "references" / "setup.md").read_bytes() == b"pi reference\n"
    if directory != "default":
        assert not (home / default_dir).exists()

    result = subprocess.run(
        ["bash", str(installer), "--uninstall", "--yes"],
        env=env, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert not target.exists()
    assert agent_dir.is_dir()
    assert shared_skill.read_text(encoding="utf-8") == "shared skill\n"


def test_installer_advertises_supported_harnesses() -> None:
    installer = Path(__file__).resolve().parents[1].parent / "scripts" / "install.sh"
    text = installer.read_text(encoding="utf-8")

    assert "antigravity" in text
    assert "cursor" in text
    assert "qoder-work" in text
    assert "kimi-code" in text
    assert "kimi-work" in text
    assert "zcode" in text
    assert "pi" in text
    assert "deepseek" in text


def test_powershell_installer_uses_the_published_package_not_an_editable_checkout() -> None:
    # `inspire update` decides whether it can upgrade itself by looking for
    # `uv/tools` or `pipx/venvs` in sys.prefix. An editable install has neither,
    # so `pip install -e` here would silently cost the user self-update.
    text = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")

    assert "uv tool install --force --refresh" in text
    assert "pipx install --force" in text
    assert "pip install -e" not in text


def test_powershell_installer_delegates_skill_layout_to_the_cli() -> None:
    # The harness list and the codex agents/openai.yaml body live in update.py;
    # a second copy in PowerShell would be a place for them to drift.
    text = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")

    assert "_refresh-skills" in text
    assert "_ensure-playwright-runtime" in text
    assert "openai.yaml" not in text


def test_powershell_installer_flags_are_separable() -> None:
    # -SkipPlaywright must not also drop the skills, and vice versa, which is
    # why each step has its own CLI hook rather than sharing `_post-update`.
    text = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")

    assert "if (-not $SkipPlaywright) {" in text
    assert "if (-not $SkipSkill) {" in text
    assert "_post-update" not in text


def test_powershell_installer_documents_the_openssh_prerequisite() -> None:
    # Every SSH-backed command needs it and it is not installed by default on
    # older Windows builds.
    text = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")

    assert "OpenSSH.Client" in text
    assert "job logs --source ssh" in text
    assert "job logs --transport ssh" not in text


@pytest.mark.skipif(sys.platform == "win32", reason="drives the bash installer end to end")
@pytest.mark.parametrize("installer", ["uv", "pipx"])
@pytest.mark.parametrize("index_mode", ["default", "environment", "legacy", "flag", "equals"])
def test_cli_only_install_and_package_index(
    tmp_path: Path, installer: str, index_mode: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Isolate command discovery, including from a system-wide uv or pipx.
    for command in ("bash", "mkdir", "chmod", "cat"):
        executable = shutil.which(command)
        assert executable
        (bin_dir / command).symlink_to(executable)
    manager = bin_dir / installer
    manager.write_text(
        '#!/usr/bin/env bash\n'
        'if [[ "$*" == *install\\ --force* ]]; then\n'
        '  printf "%s\\n" "${UV_DEFAULT_INDEX-}" "${UV_INDEX_URL-}" '
        '"${PIP_INDEX_URL-}" > "$HOME/index-env"\n'
        '  mkdir -p "$HOME/.local/bin"\n'
        '  printf \'#!/usr/bin/env bash\\nprintf "%%s\\\\n" "$*" >> "$HOME/cli-calls"\\n\' '
        '> "$HOME/.local/bin/inspire"\n'
        '  chmod +x "$HOME/.local/bin/inspire"\n'
        'fi\n',
        encoding="utf-8",
    )
    manager.chmod(0o755)
    env = {"HOME": str(home), "PATH": str(bin_dir)}
    old_index = "https://old-user:old-password@old.example/simple/"
    new_index = "https://new-user:new-p@ssword@mirror.example/simple/"
    args = []
    expected = ["", "", ""]
    if index_mode != "default":
        env.update(UV_INDEX_URL=old_index, PIP_INDEX_URL=old_index)
        expected = ["", old_index, old_index]
        if index_mode != "legacy":
            env["UV_DEFAULT_INDEX"] = old_index
            expected[0] = old_index
    if index_mode in ("flag", "equals"):
        args = ["--index-url", new_index] if index_mode == "flag" else [f"--index-url={new_index}"]
        expected = [new_index, old_index, new_index]
    result = subprocess.run(
        [str(bin_dir / "bash"), str(SCRIPTS / "install.sh"), "--no-schedule", *args],
        env=env, text=True, capture_output=True, timeout=20,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert (home / "index-env").read_text().splitlines() == expected
    assert "_ensure-playwright-runtime" in (home / "cli-calls").read_text()
    assert "no agent harness detected" in output
    assert "Skill was not installed" in output
    assert "--harness <name>" in output
    assert not (home / ".codex").exists()
    assert "fetching skill bundle" not in output
    assert "90 MB" in output and "150 MB" in output
    assert "--index-url" in output
    assert "inspire login" in output and "account add" not in output
    assert "old-user" not in output and "old-password" not in output
    assert "new-user" not in output and "new-p@ssword" not in output
    if index_mode != "default":
        host = "mirror.example" if args else "old.example"
        assert f"package index: https://{host}/simple/" in output
    else:
        assert "package index:" not in output


@pytest.mark.skipif(sys.platform == "win32", reason="drives the bash installer end to end")
@pytest.mark.parametrize("args", [["--no-cli"], ["--index-url"], ["--index-url="]])
def test_installer_rejects_nothing_to_install_or_missing_index(
    tmp_path: Path, args: list[str],
) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPTS / "install.sh"), *args],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode != 0
    assert "nothing to install" in result.stderr or "requires a URL" in result.stderr


def test_powershell_installer_supports_package_indexes() -> None:
    text = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
    assert "[string]$IndexUrl" in text
    assert "$env:UV_DEFAULT_INDEX = $IndexUrl" in text
    assert "$env:PIP_INDEX_URL = $IndexUrl" in text
    assert "$env:UV_INDEX_URL" in text
    assert "90 MB" in text and "150 MB" in text
    assert "skill files installed" not in text
