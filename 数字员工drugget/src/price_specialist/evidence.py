from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .schemas import CollectionResult, CollectionTaskSpec


SENSITIVE_KEYS = {
    "cookie",
    "cookies",
    "contact",
    "nickname",
    "password",
    "pin",
    "token",
    "user_id",
}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(data)
            temp_path = Path(handle.name)
        os.replace(temp_path, path)

    def save(self, task: CollectionTaskSpec, result: CollectionResult) -> tuple[Path, str]:
        directory = self.root / task.run_id / task.task_id
        payload = {
            "task": redact(task.model_dump(mode="json")),
            "result": redact(result.model_dump(mode="json", exclude={"evidence"})),
            "evidence": redact(result.evidence.model_dump(mode="json", exclude={"screenshot_bytes_b64"})),
        }
        metadata = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._atomic_write(directory / "metadata.json", metadata)
        raw_fields = json.dumps(redact(result.evidence.raw_fields), ensure_ascii=False, indent=2).encode("utf-8")
        self._atomic_write(directory / "raw_fields.json", raw_fields)
        screenshot = b""
        if result.evidence.screenshot_bytes_b64:
            screenshot = base64.b64decode(result.evidence.screenshot_bytes_b64)
            self._atomic_write(
                directory / "screenshot.png",
                screenshot,
            )
        digest = hashlib.sha256(metadata + raw_fields + screenshot).hexdigest()
        self._atomic_write(directory / "sha256.txt", (digest + "\n").encode("ascii"))
        return directory, digest
