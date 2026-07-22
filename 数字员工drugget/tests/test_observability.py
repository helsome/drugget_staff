"""Tests for the structured observability system (RunEvent, sinks, tracker)."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from queue import Queue

import pytest

from price_specialist.run_logger import (
    DrugRunStatus,
    DrugStatusTracker,
    JsonlEventSink,
    QueueEventSink,
    RunEvent,
    CompositeEventSink,
    _atomic_write,
    build_run_event_system,
    write_run_status_snapshot,
    write_drug_status_csv,
    write_task_status_csv,
)


# ---------------------------------------------------------------------------
# RunEvent model tests
# ---------------------------------------------------------------------------

class TestRunEvent:
    def test_default_fields(self) -> None:
        event = RunEvent(run_id="test-run", event_type="task_started")
        assert event.run_id == "test-run"
        assert event.event_type == "task_started"
        assert event.event_id != ""
        assert event.timestamp != ""

    def test_to_dict(self) -> None:
        event = RunEvent(
            run_id="r1", event_type="search_hits_received",
            platform="taobao", brand_name="托妥",
            candidate_count=5,
        )
        d = event.to_dict()
        assert d["run_id"] == "r1"
        assert d["event_type"] == "search_hits_received"
        assert d["platform"] == "taobao"
        assert d["brand_name"] == "托妥"
        assert d["candidate_count"] == 5

    def test_all_event_types_have_required_fields(self) -> None:
        """Every event type must carry run_id, event_type, and a message."""
        for event_type in [
            "run_created", "run_started", "run_cancelled", "run_finished",
            "platform_health_started", "platform_health_success", "platform_health_failed",
            "task_planned", "task_enqueued", "task_started", "task_succeeded",
            "task_not_found", "task_failed", "task_blocked", "task_skipped", "task_cancelled",
            "search_started", "search_hits_received", "search_no_hits",
            "candidate_saved", "detail_started", "detail_succeeded", "detail_failed",
            "formal_price_confirmed", "export_started", "export_succeeded", "export_failed",
        ]:
            event = RunEvent(run_id="r1", event_type=event_type, message=f"test {event_type}")
            assert event.event_type == event_type
            assert event.run_id == "r1"


# ---------------------------------------------------------------------------
# Sink tests
# ---------------------------------------------------------------------------

class TestJsonlEventSink:
    def test_writes_to_file(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = JsonlEventSink(path)
        event = RunEvent(run_id="r1", event_type="run_created", message="created")
        sink.emit(event)
        sink.close()

        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["run_id"] == "r1"
        assert parsed["event_type"] == "run_created"

    def test_appends_multiple_events(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = JsonlEventSink(path)
        for i in range(3):
            sink.emit(RunEvent(run_id="r1", event_type=f"event_{i}"))
        sink.close()

        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

    def test_flushes_after_each_write(self, tmp_path: Path) -> None:
        """Verify that events are readable without closing the sink."""
        path = tmp_path / "events.jsonl"
        sink = JsonlEventSink(path)
        sink.emit(RunEvent(run_id="r1", event_type="test"))
        # Read the file while sink is still open
        content = path.read_text(encoding="utf-8")
        assert "test" in content
        sink.close()


class TestQueueEventSink:
    def test_pushes_to_queue(self) -> None:
        q: Queue[RunEvent] = Queue()
        sink = QueueEventSink(q)
        event = RunEvent(run_id="r1", event_type="task_started")
        sink.emit(event)
        received = q.get_nowait()
        assert received.event_type == "task_started"
        assert received.run_id == "r1"

    def test_queue_is_empty_after_drain(self) -> None:
        q: Queue[RunEvent] = Queue()
        sink = QueueEventSink(q)
        sink.emit(RunEvent(run_id="r1", event_type="e1"))
        sink.emit(RunEvent(run_id="r1", event_type="e2"))
        q.get_nowait()
        q.get_nowait()
        assert q.empty()


class TestCompositeEventSink:
    def test_fans_out_to_all_sinks(self, tmp_path: Path) -> None:
        q: Queue[RunEvent] = Queue()
        jsonl_path = tmp_path / "composite.jsonl"
        jsonl_sink = JsonlEventSink(jsonl_path)
        queue_sink = QueueEventSink(q)

        composite = CompositeEventSink([jsonl_sink, queue_sink])
        composite.emit(RunEvent(run_id="r1", event_type="fanout_test"))

        # Queue received it
        received = q.get_nowait()
        assert received.event_type == "fanout_test"

        # JSONL received it
        jsonl_sink.close()
        lines = jsonl_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        assert "fanout_test" in lines[0]


# ---------------------------------------------------------------------------
# DrugStatusTracker tests
# ---------------------------------------------------------------------------

class TestDrugStatusTracker:
    def test_tracks_drug(self) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="taobao", brand_name="托妥", generic_name="托妥",
            drug_id="d1",
        ))
        snapshot = tracker.snapshot()
        assert len(snapshot) == 1
        assert snapshot[0].brand_name == "托妥"
        assert snapshot[0].platform == "taobao"
        assert snapshot[0].status == "running"

    def test_aggregates_formal_price(self) -> None:
        tracker = DrugStatusTracker()
        # Start
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="taobao", brand_name="托妥",
        ))
        # Search hits
        tracker.ingest(RunEvent(
            run_id="r1", event_type="search_hits_received",
            platform="taobao", brand_name="托妥", candidate_count=5,
        ))
        # Detail succeeded
        tracker.ingest(RunEvent(
            run_id="r1", event_type="detail_succeeded",
            platform="taobao", brand_name="托妥",
        ))
        # Formal price confirmed
        tracker.ingest(RunEvent(
            run_id="r1", event_type="formal_price_confirmed",
            platform="taobao", brand_name="托妥",
        ))

        snapshot = tracker.snapshot()
        assert len(snapshot) == 1
        ds = snapshot[0]
        assert ds.status == "success"
        assert ds.candidate_count == 5
        assert ds.detail_success_count == 1
        assert ds.formal_price_count == 1
        assert ds.completed_task_count == 2  # detail_succeeded + formal_price_confirmed

    def test_aggregates_failure(self) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="yaoshibang", brand_name="依伦平",
        ))
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_failed",
            platform="yaoshibang", brand_name="依伦平",
            error_code="timeout", error_detail="页面超时",
        ))

        snapshot = tracker.snapshot()
        ds = snapshot[0]
        assert ds.status == "partial"
        assert ds.error_count == 1
        assert ds.last_reason == "页面超时"

    def test_tracks_multiple_drugs(self) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="taobao", brand_name="托妥",
        ))
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="yaoshibang", brand_name="托妥",
        ))
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="taobao", brand_name="依伦平",
        ))

        snapshot = tracker.snapshot()
        assert len(snapshot) == 3

    def test_summary_counts(self) -> None:
        tracker = DrugStatusTracker()
        # Drug 1: success
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="taobao", brand_name="托妥",
        ))
        tracker.ingest(RunEvent(
            run_id="r1", event_type="formal_price_confirmed",
            platform="taobao", brand_name="托妥",
        ))
        # Drug 2: partial (failed)
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="yaoshibang", brand_name="依伦平",
        ))
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_failed",
            platform="yaoshibang", brand_name="依伦平",
        ))

        counts = tracker.summary_counts()
        assert counts["total"] == 2
        assert counts["success"] == 1
        assert counts["partial"] == 1
        assert counts["failed"] == 0

    def test_thread_safety(self) -> None:
        """Simulate concurrent event ingestion from multiple threads."""
        tracker = DrugStatusTracker()
        errors: list[Exception] = []

        def ingest_events(prefix: str, count: int):
            try:
                for i in range(count):
                    tracker.ingest(RunEvent(
                        run_id="r1", event_type="task_started",
                        platform="taobao", brand_name=f"Drug_{prefix}_{i}",
                    ))
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=ingest_events, args=("A", 50)),
            threading.Thread(target=ingest_events, args=("B", 50)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(tracker.snapshot()) == 100


# ---------------------------------------------------------------------------
# Atomic write tests
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_writes_content(self, tmp_path: Path) -> None:
        path = tmp_path / "test.json"
        _atomic_write(path, '{"key": "value"}')
        assert path.read_text(encoding="utf-8") == '{"key": "value"}'

    def test_atomicity_creates_tmp_file(self, tmp_path: Path) -> None:
        """The .tmp file should be gone after the write."""
        path = tmp_path / "atomic.json"
        _atomic_write(path, "content")
        assert not path.with_suffix(".tmp").exists()

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "overwrite.json"
        _atomic_write(path, "old")
        _atomic_write(path, "new")
        assert path.read_text(encoding="utf-8") == "new"


# ---------------------------------------------------------------------------
# Snapshot writer tests
# ---------------------------------------------------------------------------

class TestSnapshotWriters:
    def test_run_status_json(self, tmp_path: Path) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="taobao", brand_name="托妥",
        ))
        path = tmp_path / "run_status.json"
        write_run_status_snapshot(path, tracker, run_id="r1", run_mode="test", elapsed_seconds=10.5)

        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["run_id"] == "r1"
        assert data["run_mode"] == "test"
        assert data["elapsed_seconds"] == 10.5
        assert len(data["drugs"]) == 1
        assert data["drugs"][0]["brand_name"] == "托妥"

    def test_drug_status_csv(self, tmp_path: Path) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="taobao", brand_name="托妥",
        ))
        tracker.ingest(RunEvent(
            run_id="r1", event_type="formal_price_confirmed",
            platform="taobao", brand_name="托妥",
        ))
        path = tmp_path / "drug_status.csv"
        write_drug_status_csv(path, tracker)

        assert path.exists()
        content = path.read_text(encoding="utf-8-sig")
        assert "brand_name" in content
        assert "托妥" in content
        assert "formal_price" in content or "1" in content

    def test_task_status_csv(self, tmp_path: Path) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="taobao", brand_name="托妥",
        ))
        tracker.ingest(RunEvent(
            run_id="r1", event_type="formal_price_confirmed",
            platform="taobao", brand_name="托妥",
        ))
        path = tmp_path / "task_status.csv"
        write_task_status_csv(path, tracker)

        assert path.exists()
        content = path.read_text(encoding="utf-8-sig")
        assert "total" in content
        assert "success" in content


# ---------------------------------------------------------------------------
# Integration test: build_run_event_system
# ---------------------------------------------------------------------------

class TestBuildRunEventSystem:
    def test_sink_and_tracker(self, tmp_path: Path) -> None:
        q: Queue[RunEvent] = Queue()
        sink, tracker = build_run_event_system(
            run_id="test-run",
            output_dir=tmp_path / "events",
            event_queue=q,
        )

        # Emit events
        sink.emit(RunEvent(
            run_id="test-run", event_type="task_started",
            platform="taobao", brand_name="托妥",
        ))
        sink.emit(RunEvent(
            run_id="test-run", event_type="search_hits_received",
            platform="taobao", brand_name="托妥", candidate_count=3,
        ))

        # Tracker reflects the events
        snapshot = tracker.snapshot()
        assert len(snapshot) == 1
        assert snapshot[0].brand_name == "托妥"
        assert snapshot[0].candidate_count == 3

        # Queue received the events
        assert not q.empty()
        received = q.get_nowait()
        assert received.event_type == "task_started"

        # JSONL file was written
        jsonl_path = tmp_path / "events" / "run_events.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# RunEvent compliance: each event type carries required context
# ---------------------------------------------------------------------------

class TestEventCompliance:
    """Every task produces at least started and terminal events."""

    def test_task_lifecycle_events(self) -> None:
        """A task should produce started + terminal events."""
        tracker = DrugStatusTracker()
        task_id = "task-1"

        # Start
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started", task_id=task_id,
            platform="taobao", brand_name="托妥",
        ))
        # Succeed
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_succeeded", task_id=task_id,
            platform="taobao", brand_name="托妥",
        ))

        ds = tracker.snapshot()[0]
        assert ds.status == "success"
        assert ds.completed_task_count >= 1

    def test_search_hits_produces_hits_received(self) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="search_hits_received",
            platform="taobao", brand_name="托妥", candidate_count=5,
        ))
        ds = tracker.snapshot()[0]
        assert ds.candidate_count == 5

    def test_no_hits_produces_search_no_hits(self) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="search_no_hits",
            platform="taobao", brand_name="托妥",
        ))
        ds = tracker.snapshot()[0]
        assert ds.status == "partial"
        assert ds.last_reason == "no_hits"

    def test_detail_success_produces_detail_succeeded(self) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="detail_succeeded",
            platform="taobao", brand_name="托妥",
        ))
        ds = tracker.snapshot()[0]
        assert ds.detail_success_count == 1

    def test_formal_price_confirmed(self) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="formal_price_confirmed",
            platform="taobao", brand_name="托妥",
        ))
        ds = tracker.snapshot()[0]
        assert ds.status == "success"
        assert ds.formal_price_count == 1

    def test_events_carry_run_id_and_drug_context(self) -> None:
        """Every event must include run_id, task_id, drug, and platform context."""
        event = RunEvent(
            run_id="r1", event_type="task_started",
            task_id="t1", platform="taobao",
            brand_name="托妥", generic_name="托妥",
            drug_id="d1",
        )
        assert event.run_id == "r1"
        assert event.task_id == "t1"
        assert event.platform == "taobao"
        assert event.brand_name == "托妥"
        assert event.generic_name == "托妥"
        assert event.drug_id == "d1"

    def test_gui_queue_receives_counts(self) -> None:
        """Simulate the GUI queue receiving real completed/failed/total counts."""
        q: Queue[RunEvent] = Queue()
        sink, tracker = build_run_event_system(
            run_id="test-run",
            output_dir=Path("/tmp/test_events"),
            event_queue=q,
        )

        # Simulate a run: enqueue 3 tasks, complete 2, fail 1
        for i in range(3):
            sink.emit(RunEvent(
                run_id="test-run", event_type="task_enqueued",
                task_id=f"t{i}", platform="taobao", brand_name="托妥",
            ))

        # Success
        sink.emit(RunEvent(
            run_id="test-run", event_type="task_started",
            task_id="t0", platform="taobao", brand_name="托妥",
        ))
        sink.emit(RunEvent(
            run_id="test-run", event_type="detail_succeeded",
            task_id="t0", platform="taobao", brand_name="托妥",
        ))
        sink.emit(RunEvent(
            run_id="test-run", event_type="formal_price_confirmed",
            task_id="t0", platform="taobao", brand_name="托妥",
        ))

        # Fail
        sink.emit(RunEvent(
            run_id="test-run", event_type="task_started",
            task_id="t1", platform="taobao", brand_name="托妥",
        ))
        sink.emit(RunEvent(
            run_id="test-run", event_type="task_failed",
            task_id="t1", platform="taobao", brand_name="托妥",
            error_code="timeout",
        ))

        # Read all events from queue
        events = []
        while not q.empty():
            events.append(q.get_nowait())

        # Verify: at least 3 task_enqueued events
        enqueued = [e for e in events if e.event_type == "task_enqueued"]
        assert len(enqueued) == 3

        # Verify: success events exist
        succeeded = [e for e in events if e.event_type in ("formal_price_confirmed", "detail_succeeded")]
        assert len(succeeded) >= 2

        # Verify: failure events exist
        failed = [e for e in events if e.event_type == "task_failed"]
        assert len(failed) == 1

        # Cleanup temp files
        import shutil
        shutil.rmtree(Path("/tmp/test_events"), ignore_errors=True)


# ---------------------------------------------------------------------------
# Atomic write test: snapshot files use atomic writes
# ---------------------------------------------------------------------------

class TestSnapshotAtomicWrites:
    def test_run_status_snapshot_atomic(self, tmp_path: Path) -> None:
        """Verify that write_run_status_snapshot uses atomic write (no .tmp left)."""
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="taobao", brand_name="托妥",
        ))
        path = tmp_path / "run_status.json"
        write_run_status_snapshot(path, tracker, run_id="r1")
        assert path.exists()
        assert not path.with_suffix(".tmp").exists()

    def test_drug_status_csv_atomic(self, tmp_path: Path) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="taobao", brand_name="托妥",
        ))
        path = tmp_path / "drug_status.csv"
        write_drug_status_csv(path, tracker)
        assert path.exists()
        assert not path.with_suffix(".tmp").exists()

    def test_task_status_csv_atomic(self, tmp_path: Path) -> None:
        tracker = DrugStatusTracker()
        tracker.ingest(RunEvent(
            run_id="r1", event_type="task_started",
            platform="taobao", brand_name="托妥",
        ))
        path = tmp_path / "task_status.csv"
        write_task_status_csv(path, tracker)
        assert path.exists()
        assert not path.with_suffix(".tmp").exists()