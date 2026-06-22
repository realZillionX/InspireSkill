from __future__ import annotations

import os
from pathlib import Path
import subprocess


def test_installer_uses_installed_inspire_for_browser_runtime_setup() -> None:
    installer = Path(__file__).resolve().parents[1].parent / "scripts" / "install.sh"
    text = installer.read_text(encoding="utf-8")

    assert '"$INSPIRE_BIN" _ensure-playwright-runtime' in text
    assert 'uvx --from "$SPEC" playwright' not in text
    assert "Manual repair command" not in text


def test_installer_first_uv_install_without_inspire_on_path(tmp_path: Path) -> None:
    installer = Path(__file__).resolve().parents[1].parent / "scripts" / "install.sh"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()

    (home / ".codex").mkdir()
    (home / ".gemini").mkdir()
    (home / ".cursor").mkdir()
    (home / ".kimi-code").mkdir()
    legacy_gemini_skill = home / ".gemini" / "skills" / "inspire"
    legacy_gemini_skill.mkdir(parents=True)
    (legacy_gemini_skill / "SKILL.md").write_text("# stale\n", encoding="utf-8")
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
    }
    result = subprocess.run(
        [
            "bash",
            str(installer),
            "--harness",
            "codex,antigravity,cursor,qoder,kimi-code",
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
    assert (home / ".gemini" / "config" / "skills" / "inspire" / "SKILL.md").exists()
    assert not (home / ".gemini" / "skills" / "inspire").exists()
    assert (home / ".cursor" / "skills" / "inspire" / "SKILL.md").exists()
    assert (home / ".qoder" / "skills" / "inspire" / "SKILL.md").exists()
    assert (home / ".kimi-code" / "skills" / "inspire" / "SKILL.md").exists()


def test_installer_advertises_antigravity_not_gemini_cli() -> None:
    installer = Path(__file__).resolve().parents[1].parent / "scripts" / "install.sh"
    text = installer.read_text(encoding="utf-8")

    assert "antigravity" in text
    assert "cursor" in text
    assert "kimi-code" in text
    assert "gemini)" not in text
    assert 'legacy_target="$HOME/.gemini/skills/inspire"' in text
    assert '  target="$HOME/.gemini/skills/inspire"' not in text
