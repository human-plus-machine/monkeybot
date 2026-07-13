import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _assert_no_duplicate_force_include(pyproject_path: Path) -> None:
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    force_include = wheel.get("force-include", {})

    for package in wheel.get("packages", []):
        package_path = Path(package)
        package_name = package_path.name

        for source, destination in force_include.items():
            source_path = Path(source)
            destination_path = Path(destination)

            assert not (
                source_path.is_relative_to(package_path)
                and destination_path.is_relative_to(package_name)
            ), (
                f"{pyproject_path}: {source!r} is already included by package {package!r}; "
                f"force-including it as {destination!r} duplicates wheel files"
            )


def test_wheel_force_include_does_not_duplicate_packaged_files() -> None:
    _assert_no_duplicate_force_include(ROOT / "pyproject.toml")
    _assert_no_duplicate_force_include(ROOT / "cli" / "pyproject.toml")


def test_cli_wheel_includes_scaffold_defaults(tmp_path: Path) -> None:
    """Published CLI wheel must ship scaffold_defaults for ``monkeybot new``."""
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")

    out = tmp_path / "dist"
    proc = subprocess.run(
        ["uv", "build", "--out-dir", str(out), "cli"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    wheels = list(out.glob("monkeybot_cli-*.whl"))
    assert len(wheels) == 1, list(out.iterdir())

    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
        assert any(n.endswith("scaffold_defaults/AGENT.md") for n in names)
        assert any(n.endswith("scaffold_defaults/monkeybot.example.yaml") for n in names)
        assert any(n.endswith("scaffold_defaults/env.example") for n in names)
        meta = next(n for n in names if n.endswith("METADATA"))
        requires = [
            line
            for line in zf.read(meta).decode().splitlines()
            if line.startswith("Requires-Dist: monkeybot")
        ]
        assert any("monkeybot[cli]" in line for line in requires)
        assert any(">=2.1.0" in line and "<3" in line for line in requires)
