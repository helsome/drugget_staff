import json

from price_specialist.enums import CollectionStatus, TaskType
from price_specialist.evidence import EvidenceStore
from price_specialist.schemas import CollectionResult, CollectionTaskSpec, EvidenceBundle


def test_evidence_is_redacted_and_hashed(tmp_path) -> None:
    task = CollectionTaskSpec(
        task_id="task",
        run_id="run",
        platform="jd",
        task_type=TaskType.FIXED_CORE,
        session_alias="persistent",
    )
    result = CollectionResult(
        collection_status=CollectionStatus.CHALLENGE_DETECTED,
        evidence=EvidenceBundle(raw_fields={"token": "secret", "nested": {"cookie": "secret"}}),
    )
    directory, digest = EvidenceStore(tmp_path).save(task, result)
    payload = json.loads((directory / "raw_fields.json").read_text(encoding="utf-8"))
    assert payload["token"] == "[REDACTED]"
    assert payload["nested"]["cookie"] == "[REDACTED]"
    assert (directory / "sha256.txt").read_text().strip() == digest

