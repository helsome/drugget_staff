from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    project_dir: Path
    database_url: str
    evidence_dir: Path
    output_dir: Path
    source_dir: Path
    opencli_bin: str
    allowed_platforms: tuple[str, ...]
    dry_run_notifications: bool
    network_retry_limit: int

    @classmethod
    def from_env(cls, project_dir: Path | None = None) -> "Settings":
        root = (project_dir or Path.cwd()).resolve()

        def resolve_path(name: str, default: str) -> Path:
            value = Path(os.getenv(name, default))
            return value if value.is_absolute() else (root / value).resolve()

        allowed = tuple(
            item.strip()
            for item in os.getenv("PRICE_SPECIALIST_ALLOWED_PLATFORMS", "jd,taobao,yaoshibang").split(",")
            if item.strip()
        )
        database_url = os.getenv("PRICE_SPECIALIST_DATABASE_URL", "sqlite:///./price_specialist.db")
        if database_url.startswith("sqlite:///./"):
            database_path = (root / database_url.removeprefix("sqlite:///")).resolve()
            database_url = f"sqlite:///{database_path}"
        return cls(
            project_dir=root,
            database_url=database_url,
            evidence_dir=resolve_path("PRICE_SPECIALIST_EVIDENCE_DIR", "evidence"),
            output_dir=resolve_path("PRICE_SPECIALIST_OUTPUT_DIR", "outputs"),
            source_dir=resolve_path("PRICE_SPECIALIST_SOURCE_DIR", "过往抓取数据"),
            opencli_bin=os.getenv("PRICE_SPECIALIST_OPENCLI_BIN", "opencli"),
            allowed_platforms=allowed,
            dry_run_notifications=os.getenv("PRICE_SPECIALIST_DRY_RUN_NOTIFICATIONS", "true").lower()
            not in {"0", "false", "no"},
            network_retry_limit=int(os.getenv("PRICE_SPECIALIST_NETWORK_RETRY_LIMIT", "2")),
        )
