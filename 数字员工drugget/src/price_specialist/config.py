"""Environment-aware configuration with strict mode isolation.

Usage
-----
    from price_specialist.config import Settings

    # Explicit (preferred)
    cfg = Settings.load(project_dir, mode="test")
    cfg = Settings.load(project_dir, mode="test", overrides={"PRICE_SPECIALIST_DATABASE_URL": "sqlite:///..."})

    # Backward compatible
    cfg = Settings.from_env(project_dir, test_mode=True)

Priority (highest to lowest)
    1. ``overrides`` dict passed to ``load()``
    2. Mode-specific ``.env.{mode}`` file (e.g. ``.env.test``, ``.env.prod``)
    3. Common ``.env`` file
    4. ``os.environ`` (system environment variables)
    5. Hard-coded default values

No ``os.environ`` mutation — every call is isolated.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


# ── helpers ──────────────────────────────────────────────────────────────────


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``.env``-format file into a dict (pure, no side-effects).

    Skips missing files, comments, and malformed lines.
    """
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _load_dotenv(env_path: Path) -> None:
    """Legacy helper — mutates ``os.environ`` via ``setdefault``.

    Kept for external callers that depend on the side-effect.  New code
    should use ``Settings.load()`` which is side-effect-free.
    """
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _mask_db_url(url: str) -> str:
    """Mask password in a database URL for safe display."""
    # postgresql+psycopg://user:password@host:port/db
    masked = re.sub(r"(://[^:]+:)([^@]+)(@)", r"\1****\3", url)
    return masked


# ── Settings ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Settings:
    """Immutable configuration snapshot.

    Create via ``Settings.load()`` (preferred) or ``Settings.from_env()``
    (backward-compatible).
    """

    project_dir: Path
    database_url: str
    evidence_dir: Path
    output_dir: Path
    source_dir: Path
    opencli_bin: str
    allowed_platforms: tuple[str, ...]
    dry_run_notifications: bool
    network_retry_limit: int

    # ── public API ──────────────────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        project_dir: Path | None = None,
        *,
        mode: str | None = None,
        overrides: dict[str, str] | None = None,
    ) -> Settings:
        """Load settings with explicit priority isolation.

        Parameters
        ----------
        project_dir :
            Project root directory.  Defaults to ``cwd``.
        mode :
            ``"test"`` loads ``.env.test`` on top of ``.env``.
            ``"prod"`` loads ``.env.prod`` on top of ``.env``.
            ``None`` loads only ``.env``.
        overrides :
            Highest-priority key/value pairs (e.g. from GUI or CLI).

        Returns
        -------
        Settings
            Frozen configuration dataclass.
        """
        root = (project_dir or Path.cwd()).resolve()
        overrides = overrides or {}

        # Build value dict with layered priority (lowest first).
        values: dict[str, str] = {}

        # 1. System environment (lowest configurable source)
        for key, value in os.environ.items():
            if key.startswith("PRICE_SPECIALIST_"):
                values[key] = value

        # 2. Common .env file
        values.update(_parse_env_file(root / ".env"))

        # 3. Mode-specific .env.{mode} file
        if mode:
            values.update(_parse_env_file(root / f".env.{mode}"))

        # 4. Explicit overrides (highest priority)
        values.update({k: str(v) for k, v in overrides.items()})

        return cls._from_values(values, root)

    @classmethod
    def from_env(
        cls,
        project_dir: Path | None = None,
        *,
        test_mode: bool = False,
    ) -> Settings:
        """Backward-compatible wrapper — delegates to ``load()``.

        ``test_mode=True``  → ``load(mode="test")``
        ``test_mode=False`` → ``load(mode="prod")``  (NB: NOT ``mode=None``)
        """
        mode = "test" if test_mode else "prod"
        return cls.load(project_dir, mode=mode)

    # ── runtime safety ──────────────────────────────────────────────────────

    def validate_runtime_mode(self, mode: str) -> None:
        """Verify that the resolved settings match the expected mode.

        Checks are performed on the **relative** path from ``project_dir``
        to avoid false positives from system temp directories (e.g. pytest's
        ``tmp_path`` which contains ``"test"``).

        Raises
        ------
        RuntimeError
            If any value does not match the expected mode.
        """
        errors: list[str] = []
        db_lower = self.database_url.lower()

        # Check relative paths from project_dir to avoid false positives
        # from pytest tmp_path or other system paths containing "test".
        def _relative(p: Path) -> str:
            try:
                return str(p.relative_to(self.project_dir))
            except ValueError:
                return str(p)

        ev_rel = _relative(self.evidence_dir).lower()
        out_rel = _relative(self.output_dir).lower()

        if mode == "test":
            if "test" not in db_lower:
                errors.append(
                    f"测试模式数据库 URL 不包含 'test': {_mask_db_url(self.database_url)}"
                )
            if "test" not in ev_rel:
                errors.append(
                    f"测试模式证据目录不包含 'test': {self.evidence_dir}"
                )
            if "test" not in out_rel:
                errors.append(
                    f"测试模式输出目录不包含 'test': {self.output_dir}"
                )
        elif mode == "prod":
            if "test" in db_lower:
                errors.append(
                    f"正式模式数据库 URL 包含 'test': {_mask_db_url(self.database_url)}"
                )
            if "test" in ev_rel:
                errors.append(
                    f"正式模式证据目录包含 'test': {self.evidence_dir}"
                )
            if "test" in out_rel:
                errors.append(
                    f"正式模式输出目录包含 'test': {self.output_dir}"
                )

        if errors:
            raise RuntimeError(
                f"运行模式验证失败 (mode={mode}):\n" + "\n".join(errors)
            )

    def masked_display(self) -> dict[str, str]:
        """Return a dict of settings with sensitive values masked.

        Suitable for display in GUI or logs.
        """
        result: dict[str, str] = {}
        for field_name in (
            "project_dir",
            "database_url",
            "evidence_dir",
            "output_dir",
            "source_dir",
            "opencli_bin",
            "allowed_platforms",
            "dry_run_notifications",
            "network_retry_limit",
        ):
            raw = getattr(self, field_name)
            if field_name == "database_url":
                result[field_name] = _mask_db_url(str(raw))
            elif isinstance(raw, Path):
                result[field_name] = str(raw)
            elif isinstance(raw, tuple):
                result[field_name] = ",".join(raw)
            else:
                result[field_name] = str(raw)
        return result

    # ── internal helpers ────────────────────────────────────────────────────

    @classmethod
    def _from_values(cls, values: dict[str, str], root: Path) -> Settings:
        """Build ``Settings`` from a resolved value dict."""

        def resolve_path(name: str, default: str) -> Path:
            raw = values.get(name, default)
            p = Path(raw)
            return p if p.is_absolute() else (root / p).resolve()

        allowed = tuple(
            item.strip()
            for item in values.get(
                "PRICE_SPECIALIST_ALLOWED_PLATFORMS", "jd,taobao,yaoshibang"
            ).split(",")
            if item.strip()
        )

        database_url = values.get(
            "PRICE_SPECIALIST_DATABASE_URL", "sqlite:///./price_specialist.db"
        )
        # Resolve relative sqlite paths to absolute
        if database_url.startswith("sqlite:///./"):
            db_path = (root / database_url.removeprefix("sqlite:///")).resolve()
            database_url = f"sqlite:///{db_path}"

        return cls(
            project_dir=root,
            database_url=database_url,
            evidence_dir=resolve_path("PRICE_SPECIALIST_EVIDENCE_DIR", "artifacts/evidence"),
            output_dir=resolve_path("PRICE_SPECIALIST_OUTPUT_DIR", "outputs"),
            source_dir=resolve_path("PRICE_SPECIALIST_SOURCE_DIR", "data/raw"),
            opencli_bin=values.get("PRICE_SPECIALIST_OPENCLI_BIN", "opencli"),
            allowed_platforms=allowed,
            dry_run_notifications=values.get("PRICE_SPECIALIST_DRY_RUN_NOTIFICATIONS", "true").lower()
            not in {"0", "false", "no"},
            network_retry_limit=int(values.get("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT", "2")),
        )