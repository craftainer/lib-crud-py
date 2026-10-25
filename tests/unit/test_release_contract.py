"""Makefile release contract checks ported from template-python-package v1.3.2.

Covers the registry-env isolation and the pinned uv image used by
`verify-published`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")


def make(
    *args: str, publish: str | None = None, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run make in the repo root with PYPI_PUBLISH set to `publish` (or unset)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYPI_PUBLISH", "NPM_PUBLISH", "CARGO_PUBLISH"}
    }
    env.update(extra_env or {})
    if publish is not None:
        env["PYPI_PUBLISH"] = publish
    return subprocess.run(  # noqa: S603 -- fixed argv, trusted tool
        ["make", "-C", str(ROOT), *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("other", ["NPM_PUBLISH", "CARGO_PUBLISH"])
def test_unset_pypi_does_not_warn_when_another_registry_publishes(other: str) -> None:
    """a release publishing only to npm or crates.io is not told PyPI was skipped."""
    result = make("-s", "publish-packages", extra_env={other: "1"})
    assert result.returncode == 0
    assert "::warning" not in result.stdout
    assert "skipping the PyPI upload" in result.stdout


def _makefile_var(name: str) -> str:
    match = re.search(rf"^{name} := (\S+)$", (ROOT / "Makefile").read_text(), re.M)
    assert match, name
    return match.group(1)


def test_makefile_pins_match_the_devcontainer() -> None:
    """the uv image uses the same uv version and Python minor as the devcontainer."""
    dockerfile = (ROOT / ".devcontainer" / "Dockerfile").read_text()
    uv = re.search(r"^ARG UV_VERSION=(\S+)$", dockerfile, re.M)
    python = re.search(r"^ARG PYTHON_VERSION=(\S+)$", dockerfile, re.M)
    assert uv
    assert python
    assert _makefile_var("UV_VERSION") == uv.group(1)
    assert python.group(1).startswith(f"{_makefile_var('PYTHON_VERSION')}.")


def test_verify_published_runs_in_the_pinned_uv_image() -> None:
    """the script gets the pinned image, so the runner needs no Python of its own."""
    result = make("-n", "verify-published", publish="pypi")
    assert result.returncode == 0
    assert "UV_IMAGE=ghcr.io/astral-sh/uv:" in result.stdout
    assert f"python{_makefile_var('PYTHON_VERSION')}-" in result.stdout


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_verify_published_fails_fast_when_no_interpreter_satisfies_requires_python(
    tmp_path: Path,
) -> None:
    """an unsatisfiable requires-python is reported at once, not as a missing release."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "release-verify-published.sh", tmp_path / "scripts")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "pkg-1.0.0-py3-none-any.whl").write_bytes(b"")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\nversion = "0"\nrequires-python = ">=99"\n'
    )
    env = {
        **{k: v for k, v in os.environ.items() if k not in {"VERIFY_IN_CONTAINER", "UV_PYTHON"}},
        "PYPI_PUBLISH": "pypi",
        "RELEASE_VERSION": "1.0.0",
        "RELEASE_CHECK_DELAY": "0",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    result = subprocess.run(  # noqa: S603 -- fixed argv, trusted script
        ["bash", str(tmp_path / "scripts" / "release-verify-published.sh")],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode != 0
    assert "requires-python '>=99'" in result.stderr
    assert "No interpreter found" in result.stderr
    assert "never appeared" not in result.stderr
