from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from price_specialist.api import create_app
from price_specialist.config import Settings
from price_specialist.database import create_db_engine, init_database, make_session_factory
from price_specialist.models import CollectionRun, CollectionTask, Incident


def test_health_readiness_and_incident_workflow(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    settings = Settings(
        project_dir=tmp_path,
        database_url=database_url,
        evidence_dir=tmp_path / "evidence",
        output_dir=tmp_path / "outputs",
        source_dir=tmp_path,
        opencli_bin="opencli",
        allowed_platforms=("jd", "taobao"),
        dry_run_notifications=True,
        network_retry_limit=2,
    )
    engine = create_db_engine(database_url)
    init_database(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        run = CollectionRun(id="run-1")
        task = CollectionTask(
            id="task-1",
            run_id="run-1",
            platform="jd",
            task_type="fixed_core",
            session_alias="persistent",
        )
        incident = Incident(
            id="incident-1",
            task_id="task-1",
            platform="jd",
            incident_type="challenge_detected",
            session_alias="persistent",
            detected_at=datetime.now(),
        )
        session.add_all([run, task, incident])

    client = TestClient(create_app(settings))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200
    response = client.get("/v1/incidents?limit=10")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    response = client.patch("/v1/incidents/incident-1", json={"action": "in_progress", "operator_note": "人工接管"})
    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"
    invalid = client.patch("/v1/incidents/incident-1", json={"action": "resolved"})
    assert invalid.status_code == 409

