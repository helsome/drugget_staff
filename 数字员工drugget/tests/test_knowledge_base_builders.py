from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_script(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{filename or name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_builder_help_has_no_output_side_effects() -> None:
    published = [
        ROOT / "data/knowledge-base/manifest.json",
        ROOT / "data/knowledge-base/README.md",
        ROOT / "data/fixtures/业务知识库测试集/summary.json",
        ROOT / "data/fixtures/业务知识库测试集/price_specialist_test.sqlite3",
    ]
    before = {path: path.stat().st_mtime_ns for path in published}
    for script in ("build_knowledge_base.py", "build_test_knowledge_base.py"):
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / script), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "usage:" in completed.stdout
    assert {path: path.stat().st_mtime_ns for path in published} == before


def test_fixture_build_is_consistent_and_atomic(tmp_path: Path) -> None:
    fixture_builder = load_script("build_test_knowledge_base")
    output = tmp_path / "fixture"
    summary = fixture_builder.build_atomically(ROOT / "data/knowledge-base", output)

    assert (output / "price_specialist_test.sqlite3").is_file()
    assert json.loads((output / "summary.json").read_text(encoding="utf-8")) == summary
    with sqlite3.connect(output / "price_specialist_test.sqlite3") as connection:
        version = connection.execute(
            "SELECT value FROM fixture_info WHERE key = 'fixture_version'"
        ).fetchone()[0]
    assert version == summary["fixture_version"]


def test_fixture_failure_does_not_replace_published_output(tmp_path: Path) -> None:
    fixture_builder = load_script(
        "build_test_knowledge_base_failure", "build_test_knowledge_base"
    )
    output = tmp_path / "fixture"
    output.mkdir()
    sentinel = output / "price_specialist_test.sqlite3"
    sentinel.write_text("known-good", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        fixture_builder.build_atomically(tmp_path / "missing-source", output)

    assert sentinel.read_text(encoding="utf-8") == "known-good"
