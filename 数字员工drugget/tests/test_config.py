"""Tests for config module — environment isolation and mode validation."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from price_specialist.config import Settings, _parse_env_file, _mask_db_url


# ── _parse_env_file ──────────────────────────────────────────────────────────


class TestParseEnvFile:
    def test_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        assert _parse_env_file(tmp_path / ".env.nonexistent") == {}

    def test_parses_valid_lines(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text(
            "KEY=value\n"
            "# comment\n"
            "EMPTY=\n"
            "  SPACES  =  trimmed  \n"
        )
        result = _parse_env_file(env)
        assert result["KEY"] == "value"
        assert result["EMPTY"] == ""
        assert result["SPACES"] == "trimmed"

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("KEY=value\nno_equals_sign\n")
        result = _parse_env_file(env)
        assert result["KEY"] == "value"
        assert "no_equals_sign" not in result


# ── _mask_db_url ─────────────────────────────────────────────────────────────


class TestMaskDbUrl:
    def test_masks_password_in_postgres_url(self) -> None:
        url = "postgresql+psycopg://user:secret123@localhost:5432/db"
        assert _mask_db_url(url) == "postgresql+psycopg://user:****@localhost:5432/db"

    def test_does_not_mutate_sqlite_url(self) -> None:
        url = "sqlite:///./price_specialist.db"
        assert _mask_db_url(url) == url

    def test_masks_password_in_https_url(self) -> None:
        url = "https://user:pass@example.com/foo"
        assert _mask_db_url(url) == "https://user:****@example.com/foo"

    def test_no_password_no_change(self) -> None:
        url = "sqlite:////tmp/test.db"
        assert _mask_db_url(url) == url


# ── Settings.load() priority ─────────────────────────────────────────────────


class TestSettingsLoadPriority:
    """Test that load() respects the declared priority:

    overrides > mode .env > common .env > os.environ > defaults
    """

    def test_load_with_no_files_uses_defaults(self, tmp_path: Path) -> None:
        """No .env files at all → use hard-coded defaults."""
        settings = Settings.load(tmp_path)
        assert settings.database_url == f"sqlite:///{(tmp_path / 'price_specialist.db').resolve()}"
        assert settings.network_retry_limit == 2
        assert settings.dry_run_notifications is True

    def test_common_env_overrides_defaults(self, tmp_path: Path) -> None:
        """Common .env overrides hard-coded defaults."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=5\n")
        settings = Settings.load(tmp_path)
        assert settings.network_retry_limit == 5

    def test_system_env_is_lower_than_common_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """System env var is overridden by .env file value."""
        monkeypatch.setenv("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT", "99")
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=5\n")
        settings = Settings.load(tmp_path)
        # .env overrides os.environ
        assert settings.network_retry_limit == 5

    def test_system_env_is_used_when_no_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """System env var is used when .env file does not set the key."""
        monkeypatch.setenv("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT", "42")
        settings = Settings.load(tmp_path)
        assert settings.network_retry_limit == 42

    def test_mode_env_overrides_common_env(self, tmp_path: Path) -> None:
        """.env.test overrides .env."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=1\n")
        (tmp_path / ".env.test").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=9\n")
        settings = Settings.load(tmp_path, mode="test")
        assert settings.network_retry_limit == 9

    def test_overrides_overrides_everything(self, tmp_path: Path) -> None:
        """Explicit overrides beat mode .env, common .env, and system env."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=1\n")
        (tmp_path / ".env.test").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=2\n")
        settings = Settings.load(tmp_path, mode="test", overrides={"PRICE_SPECIALIST_NETWORK_RETRY_LIMIT": "99"})
        assert settings.network_retry_limit == 99

    def test_mode_prod_loads_prod_env(self, tmp_path: Path) -> None:
        """.env.prod is loaded for mode="prod"."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=1\n")
        (tmp_path / ".env.prod").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=8\n")
        settings = Settings.load(tmp_path, mode="prod")
        assert settings.network_retry_limit == 8

    def test_mode_none_does_not_load_mode_file(self, tmp_path: Path) -> None:
        """mode=None only loads .env, not .env.test or .env.prod."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=3\n")
        (tmp_path / ".env.test").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=9\n")
        (tmp_path / ".env.prod").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=8\n")
        settings = Settings.load(tmp_path, mode=None)
        assert settings.network_retry_limit == 3  # from .env, not from .env.test or .env.prod


# ── Mode isolation (test vs prod) ────────────────────────────────────────────


class TestModeIsolation:
    """Test that test and prod modes produce strictly different configurations."""

    def test_mode_test_uses_test_db(self, tmp_path: Path) -> None:
        """.env points to prod, .env.test points to test → test mode uses test DB."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_DATABASE_URL=sqlite:///./prod.db\n")
        (tmp_path / ".env.test").write_text("PRICE_SPECIALIST_DATABASE_URL=sqlite:///./test.db\n")
        test_settings = Settings.load(tmp_path, mode="test")
        assert "test.db" in test_settings.database_url

    def test_mode_prod_uses_prod_db(self, tmp_path: Path) -> None:
        """.env points to test, .env.prod points to prod → prod mode uses prod DB."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_DATABASE_URL=sqlite:///./test.db\n")
        (tmp_path / ".env.prod").write_text("PRICE_SPECIALIST_DATABASE_URL=sqlite:///./prod.db\n")
        prod_settings = Settings.load(tmp_path, mode="prod")
        assert "prod.db" in prod_settings.database_url

    def test_evidence_dir_differs_by_mode(self, tmp_path: Path) -> None:
        """Test and prod evidence dirs are different."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_EVIDENCE_DIR=artifacts/evidence\n")
        (tmp_path / ".env.test").write_text("PRICE_SPECIALIST_EVIDENCE_DIR=artifacts/evidence-test\n")
        test_settings = Settings.load(tmp_path, mode="test")
        prod_settings = Settings.load(tmp_path, mode="prod")
        assert test_settings.evidence_dir != prod_settings.evidence_dir
        assert "test" in str(test_settings.evidence_dir)

    def test_output_dir_differs_by_mode(self, tmp_path: Path) -> None:
        """Test and prod output dirs are different."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_OUTPUT_DIR=outputs\n")
        (tmp_path / ".env.test").write_text("PRICE_SPECIALIST_OUTPUT_DIR=outputs/test\n")
        test_settings = Settings.load(tmp_path, mode="test")
        prod_settings = Settings.load(tmp_path, mode="prod")
        assert test_settings.output_dir != prod_settings.output_dir
        assert "test" in str(test_settings.output_dir)


# ── Same-process isolation (no cross-contamination) ──────────────────────────


class TestSameProcessIsolation:
    """Test that calling load() with different modes in the same process
    does not cross-contaminate."""

    def test_test_then_prod_no_cross_contamination(self, tmp_path: Path) -> None:
        """Same process: test first, then prod — no cross-contamination."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_DATABASE_URL=sqlite:///./default.db\n")
        (tmp_path / ".env.test").write_text("PRICE_SPECIALIST_DATABASE_URL=sqlite:///./test.db\n")
        (tmp_path / ".env.prod").write_text("PRICE_SPECIALIST_DATABASE_URL=sqlite:///./prod.db\n")

        # Load test first
        test_settings = Settings.load(tmp_path, mode="test")
        assert "test.db" in test_settings.database_url

        # Then load prod — must not be contaminated by test
        prod_settings = Settings.load(tmp_path, mode="prod")
        assert "prod.db" in prod_settings.database_url

        # Test again — still uses test DB
        test_settings2 = Settings.load(tmp_path, mode="test")
        assert "test.db" in test_settings2.database_url

    def test_prod_then_test_no_cross_contamination(self, tmp_path: Path) -> None:
        """Same process: prod first, then test — no cross-contamination."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_DATABASE_URL=sqlite:///./default.db\n")
        (tmp_path / ".env.test").write_text("PRICE_SPECIALIST_DATABASE_URL=sqlite:///./test.db\n")
        (tmp_path / ".env.prod").write_text("PRICE_SPECIALIST_DATABASE_URL=sqlite:///./prod.db\n")

        # Load prod first
        prod_settings = Settings.load(tmp_path, mode="prod")
        assert "prod.db" in prod_settings.database_url

        # Then load test — must not be contaminated by prod
        test_settings = Settings.load(tmp_path, mode="test")
        assert "test.db" in test_settings.database_url

        # Prod again — still uses prod DB
        prod_settings2 = Settings.load(tmp_path, mode="prod")
        assert "prod.db" in prod_settings2.database_url

    def test_os_environ_not_mutated_by_load(self, tmp_path: Path) -> None:
        """load() does not modify os.environ."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=77\n")
        before = os.environ.get("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT")
        Settings.load(tmp_path)
        after = os.environ.get("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT")
        assert before == after

    def test_os_environ_not_mutated_by_mode_env(self, tmp_path: Path) -> None:
        """Load with mode="test" does not write .env.test values into os.environ."""
        (tmp_path / ".env.test").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=9\n")
        before = os.environ.get("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT")
        Settings.load(tmp_path, mode="test")
        after = os.environ.get("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT")
        assert before == after


# ── validate_runtime_mode ────────────────────────────────────────────────────


class TestValidateRuntimeMode:
    def test_test_mode_with_test_db_passes(self, tmp_path: Path) -> None:
        settings = Settings(
            project_dir=tmp_path,
            database_url="sqlite:///./test.db",
            evidence_dir=tmp_path / "evidence-test",
            output_dir=tmp_path / "outputs/test",
            source_dir=tmp_path / "data",
            opencli_bin="opencli",
            allowed_platforms=("taobao",),
            dry_run_notifications=True,
            network_retry_limit=2,
        )
        settings.validate_runtime_mode("test")  # should not raise

    def test_test_mode_with_prod_db_raises(self, tmp_path: Path) -> None:
        settings = Settings(
            project_dir=tmp_path,
            database_url="sqlite:///./prod.db",
            evidence_dir=tmp_path / "evidence-test",
            output_dir=tmp_path / "outputs/test",
            source_dir=tmp_path / "data",
            opencli_bin="opencli",
            allowed_platforms=("taobao",),
            dry_run_notifications=True,
            network_retry_limit=2,
        )
        with pytest.raises(RuntimeError, match="数据库 URL 不包含 'test'"):
            settings.validate_runtime_mode("test")

    def test_test_mode_with_prod_evidence_raises(self, tmp_path: Path) -> None:
        settings = Settings(
            project_dir=tmp_path,
            database_url="sqlite:///./test.db",
            evidence_dir=tmp_path / "evidence",  # no "test" in path
            output_dir=tmp_path / "outputs/test",
            source_dir=tmp_path / "data",
            opencli_bin="opencli",
            allowed_platforms=("taobao",),
            dry_run_notifications=True,
            network_retry_limit=2,
        )
        with pytest.raises(RuntimeError, match="证据目录不包含 'test'"):
            settings.validate_runtime_mode("test")

    def test_test_mode_with_prod_output_raises(self, tmp_path: Path) -> None:
        settings = Settings(
            project_dir=tmp_path,
            database_url="sqlite:///./test.db",
            evidence_dir=tmp_path / "evidence-test",
            output_dir=tmp_path / "outputs",  # no "test" in path
            source_dir=tmp_path / "data",
            opencli_bin="opencli",
            allowed_platforms=("taobao",),
            dry_run_notifications=True,
            network_retry_limit=2,
        )
        with pytest.raises(RuntimeError, match="输出目录不包含 'test'"):
            settings.validate_runtime_mode("test")

    def test_prod_mode_with_test_db_raises(self, tmp_path: Path) -> None:
        settings = Settings(
            project_dir=tmp_path,
            database_url="sqlite:///./test.db",  # test DB in prod mode
            evidence_dir=tmp_path / "evidence",
            output_dir=tmp_path / "outputs",
            source_dir=tmp_path / "data",
            opencli_bin="opencli",
            allowed_platforms=("taobao",),
            dry_run_notifications=True,
            network_retry_limit=2,
        )
        with pytest.raises(RuntimeError, match="数据库 URL 包含 'test'"):
            settings.validate_runtime_mode("prod")

    def test_prod_mode_with_prod_settings_passes(self, tmp_path: Path) -> None:
        settings = Settings(
            project_dir=tmp_path,
            database_url="sqlite:///./prod.db",
            evidence_dir=tmp_path / "evidence",
            output_dir=tmp_path / "outputs",
            source_dir=tmp_path / "data",
            opencli_bin="opencli",
            allowed_platforms=("taobao",),
            dry_run_notifications=True,
            network_retry_limit=2,
        )
        settings.validate_runtime_mode("prod")  # should not raise

    def test_validation_reports_multiple_errors(self, tmp_path: Path) -> None:
        """Test mode with all three prod values raises multiple errors."""
        settings = Settings(
            project_dir=tmp_path,
            database_url="sqlite:///./prod.db",
            evidence_dir=tmp_path / "evidence",
            output_dir=tmp_path / "outputs",
            source_dir=tmp_path / "data",
            opencli_bin="opencli",
            allowed_platforms=("taobao",),
            dry_run_notifications=True,
            network_retry_limit=2,
        )
        with pytest.raises(RuntimeError) as exc_info:
            settings.validate_runtime_mode("test")
        msg = str(exc_info.value)
        assert "数据库 URL 不包含 'test'" in msg
        assert "证据目录不包含 'test'" in msg
        assert "输出目录不包含 'test'" in msg


# ── masked_display ────────────────────────────────────────────────────────────


class TestMaskedDisplay:
    def test_masks_database_password(self, tmp_path: Path) -> None:
        settings = Settings(
            project_dir=tmp_path,
            database_url="postgresql+psycopg://user:secret@localhost:5432/db",
            evidence_dir=tmp_path / "evidence",
            output_dir=tmp_path / "outputs",
            source_dir=tmp_path / "data",
            opencli_bin="opencli",
            allowed_platforms=("taobao",),
            dry_run_notifications=True,
            network_retry_limit=2,
        )
        display = settings.masked_display()
        assert "****" in display["database_url"]
        assert "secret" not in display["database_url"]

    def test_contains_all_fields(self, tmp_path: Path) -> None:
        settings = Settings(
            project_dir=tmp_path,
            database_url="sqlite:///./test.db",
            evidence_dir=tmp_path / "evidence",
            output_dir=tmp_path / "outputs",
            source_dir=tmp_path / "data",
            opencli_bin="opencli",
            allowed_platforms=("taobao", "yaoshibang"),
            dry_run_notifications=True,
            network_retry_limit=2,
        )
        display = settings.masked_display()
        assert "database_url" in display
        assert "evidence_dir" in display
        assert "output_dir" in display
        assert "opencli_bin" in display
        assert "allowed_platforms" in display
        assert "network_retry_limit" in display


# ── from_env backward compatibility ──────────────────────────────────────────


class TestFromEnvBackwardCompatibility:
    def test_from_env_no_args_uses_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env() without args uses cwd."""
        # Just verify it doesn't crash
        settings = Settings.from_env()
        assert isinstance(settings, Settings)

    def test_from_env_defaults_match_load_prod(self, tmp_path: Path) -> None:
        """from_env(test_mode=False) should match load(mode='prod')."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=3\n")
        (tmp_path / ".env.prod").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=7\n")
        from_env_settings = Settings.from_env(tmp_path, test_mode=False)
        load_settings = Settings.load(tmp_path, mode="prod")
        assert from_env_settings.network_retry_limit == load_settings.network_retry_limit

    def test_from_env_test_mode_matches_load_test(self, tmp_path: Path) -> None:
        """from_env(test_mode=True) should match load(mode='test')."""
        (tmp_path / ".env").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=3\n")
        (tmp_path / ".env.test").write_text("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT=9\n")
        from_env_settings = Settings.from_env(tmp_path, test_mode=True)
        load_settings = Settings.load(tmp_path, mode="test")
        assert from_env_settings.network_retry_limit == load_settings.network_retry_limit


# ── Integration: real .env files in project root ─────────────────────────────


class TestWithRealProjectEnvFiles:
    """Test that the Settings.load() works correctly with the actual .env files
    in the project root directory."""

    def test_project_root_has_env_files(self) -> None:
        """The project root should have .env, .env.test, and .env.prod files."""
        project_root = Path(__file__).resolve().parents[1]
        assert (project_root / ".env").is_file()
        assert (project_root / ".env.test").is_file()
        assert (project_root / ".env.prod").is_file()

    def test_real_env_test_mode_uses_test_db(self) -> None:
        """With real project files, test mode should use test database."""
        project_root = Path(__file__).resolve().parents[1]
        settings = Settings.load(project_root, mode="test")
        assert "test" in settings.database_url.lower()

    def test_real_env_prod_mode_uses_prod_db(self) -> None:
        """With real project files, prod mode should use prod database (SQLite)."""
        project_root = Path(__file__).resolve().parents[1]
        settings = Settings.load(project_root, mode="prod")
        assert "test" not in settings.database_url.lower()

    def test_real_env_prod_evidence_differs_from_test(self) -> None:
        """Production evidence and test evidence directories are different."""
        project_root = Path(__file__).resolve().parents[1]
        test_settings = Settings.load(project_root, mode="test")
        prod_settings = Settings.load(project_root, mode="prod")
        # With real files, .env.test sets evidence-test, .env.prod sets evidence
        assert test_settings.evidence_dir != prod_settings.evidence_dir
        assert "test" in str(test_settings.evidence_dir)

    def test_real_env_output_differs_from_test(self) -> None:
        """Production output and test output directories are different."""
        project_root = Path(__file__).resolve().parents[1]
        test_settings = Settings.load(project_root, mode="test")
        prod_settings = Settings.load(project_root, mode="prod")
        assert test_settings.output_dir != prod_settings.output_dir
        assert "test" in str(test_settings.output_dir)