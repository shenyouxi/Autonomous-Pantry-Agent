# Tests for pantry/tracing.py: step recording, error steps, decision trace upserts and fail-safe MLflow.
# MLflow is never contacted: the real tracer is disabled or the mlflow module is replaced with a mock.
from __future__ import annotations

import sys
from datetime import date
from types import SimpleNamespace
from unittest import mock

import pytest

from pantry import tracing
from pantry.models import Run, Trace
from pantry.store.json_store import JsonStore
from pantry.tracing import MlflowTracer, save_decision_trace, trace_id_for_run, trace_step

START = date(2026, 11, 14)


@pytest.fixture
def store() -> JsonStore:
    s = JsonStore.memory(start_date=START)
    s.create_run(Run(run_id="run-1", trigger="EXPIRY_IMMINENT", item_name="milk", idempotency_key="k1"))
    return s


@pytest.fixture
def off() -> MlflowTracer:
    return MlflowTracer("")


def _broken_mlflow() -> mock.MagicMock:
    fake = mock.MagicMock(name="mlflow")
    fake.start_span.side_effect = RuntimeError("mlflow backend unreachable")
    fake.update_current_trace.side_effect = RuntimeError("mlflow backend unreachable")
    return fake


def test_steps_are_appended_in_order(store: JsonStore, off: MlflowTracer) -> None:
    with trace_step(store, "run-1", "Sentinel", tracer=off, item="milk"):
        pass
    with trace_step(store, "run-1", "Planner", tracer=off) as step:
        step.set(dish_name="milk pudding")
    with trace_step(store, "run-1", "Gate", tracer=off) as step:
        step.degrade("search unavailable")

    steps = store.get_run("run-1").steps
    assert [s.name for s in steps] == ["Sentinel", "Planner", "Gate"]
    assert [s.status for s in steps] == ["ok", "ok", "degraded"]
    assert steps[0].detail == {"item": "milk"}
    assert steps[1].detail == {"dish_name": "milk pudding"}
    assert steps[2].detail["degraded_reason"] == "search unavailable"
    assert steps[0].ts <= steps[1].ts <= steps[2].ts


def test_nested_steps_are_appended_when_they_finish(store: JsonStore, off: MlflowTracer) -> None:
    with trace_step(store, "run-1", "Planner", tracer=off):
        with trace_step(store, "run-1", "vector_search", tracer=off):
            pass
        with trace_step(store, "run-1", "llm", tracer=off):
            pass
    assert [s.name for s in store.get_run("run-1").steps] == ["vector_search", "llm", "Planner"]


def test_error_step_is_recorded_and_reraised(store: JsonStore, off: MlflowTracer) -> None:
    with pytest.raises(ValueError, match="boom"):
        with trace_step(store, "run-1", "Resolver", tracer=off):
            raise ValueError("boom")

    (step,) = store.get_run("run-1").steps
    assert step.name == "Resolver"
    assert step.status == "failed"
    assert step.detail["error"] == "ValueError: boom"


def test_long_detail_is_shortened(store: JsonStore, off: MlflowTracer) -> None:
    with trace_step(store, "run-1", "Resolver", tracer=off, answer="x" * 1000):
        pass
    assert len(store.get_run("run-1").steps[0].detail["answer"]) == tracing.MAX_DETAIL_CHARS


def test_checkpoint_writes_state_and_step_together(store: JsonStore, off: MlflowTracer) -> None:
    with trace_step(store, "run-1", "Planner", tracer=off) as step:
        step.checkpoint("PLANNED", {"dish_name": "milk pudding"})
    run = store.get_run("run-1")
    assert run.state == "PLANNED"
    assert run.checkpoints["PLANNED"] == {"dish_name": "milk pudding"}
    assert [s.name for s in run.steps] == ["Planner"]


def test_trace_saved_at_gated_then_updated(store: JsonStore, off: MlflowTracer) -> None:
    with trace_step(store, "run-1", "Gate", tracer=off):
        pass
    trace_id = trace_id_for_run("run-1")
    pending = Trace(trace_id=trace_id, run_id="run-1", day_index=0, trigger="EXPIRY_IMMINENT",
                    snapshot_text="milk expires in 1 day", risk_status="allergen",
                    final_outcome="pending_confirmation")
    save_decision_trace(store, pending, tracer=off)

    got = store.get_trace_by_run("run-1")
    assert got is not None and got.trace_id == trace_id
    assert got.final_outcome == "pending_confirmation"
    assert [s.name for s in got.steps] == ["Gate"]

    with trace_step(store, "run-1", "Record", tracer=off):
        pass
    save_decision_trace(store, got.model_copy(update={"final_outcome": "rejected"}), tracer=off)

    final = store.get_trace_by_run("run-1")
    assert final.trace_id == trace_id
    assert final.final_outcome == "rejected"
    assert [s.name for s in final.steps] == ["Gate", "Record"]
    assert len([t for t in store.list_traces() if t.run_id == "run-1"]) == 1


def test_mlflow_failure_does_not_affect_store_writes(store: JsonStore, caplog: pytest.LogCaptureFixture) -> None:
    tracer = MlflowTracer("http://mlflow.invalid")
    fake = _broken_mlflow()
    with mock.patch.dict(sys.modules, {"mlflow": fake}), caplog.at_level("WARNING", logger="pantry.tracing"):
        assert tracer.enabled
        with trace_step(store, "run-1", "Sentinel", tracer=tracer):
            pass
        with pytest.raises(KeyError):
            with trace_step(store, "run-1", "Planner", tracer=tracer):
                raise KeyError("milk")
        save_decision_trace(store, Trace(trace_id="t1", run_id="run-1", day_index=0, trigger="EXPIRY_IMMINENT",
                                         snapshot_text="s", final_outcome="accepted"), tracer=tracer)

    assert fake.start_span.call_count == 1  # disabled after the first failure
    assert not tracer.enabled
    assert [(s.name, s.status) for s in store.get_run("run-1").steps] == [("Sentinel", "ok"), ("Planner", "failed")]
    assert store.get_trace("t1") is not None
    assert sum("MLflow tracing disabled" in r.getMessage() for r in caplog.records) == 1


def test_mlflow_span_end_failure_keeps_body_result(store: JsonStore) -> None:
    span_cm = mock.MagicMock()
    span_cm.__enter__.return_value = mock.MagicMock()
    span_cm.__exit__.side_effect = RuntimeError("export failed")
    fake = mock.MagicMock(name="mlflow")
    fake.start_span.return_value = span_cm
    tracer = MlflowTracer("http://mlflow.invalid")
    with mock.patch.dict(sys.modules, {"mlflow": fake}):
        with trace_step(store, "run-1", "Gate", tracer=tracer):
            pass
    fake.start_span.assert_called_once_with(name="Gate")
    assert [s.name for s in store.get_run("run-1").steps] == ["Gate"]
    assert not tracer.enabled


def test_mlflow_spans_written_when_enabled(store: JsonStore) -> None:
    live = mock.MagicMock()
    span_cm = mock.MagicMock()
    span_cm.__enter__.return_value = live
    span_cm.__exit__.return_value = False
    fake = mock.MagicMock(name="mlflow")
    fake.start_span.return_value = span_cm
    tracer = MlflowTracer("http://mlflow.invalid", "exp")
    with mock.patch.dict(sys.modules, {"mlflow": fake}):
        with trace_step(store, "run-1", "Planner", tracer=tracer) as step:
            step.set(dish_name="pudding")
    fake.set_tracking_uri.assert_called_once_with("http://mlflow.invalid")
    fake.set_experiment.assert_called_once_with("exp")
    live.set_attributes.assert_any_call({"run_id": "run-1", "step": "Planner"})
    assert tracer.enabled


def test_mlflow_disabled_when_uri_unset(store: JsonStore, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(mlflow_tracking_uri="", databricks_host="", databricks_token="")
    monkeypatch.setattr(tracing, "get_settings", lambda: settings)
    monkeypatch.setattr(tracing, "_tracer", None)
    fake = mock.MagicMock(name="mlflow")
    with mock.patch.dict(sys.modules, {"mlflow": fake}):
        tracer = tracing.get_tracer()
        assert not tracer.enabled
        with trace_step(store, "run-1", "Sentinel"):
            pass
    assert fake.mock_calls == []
    assert [s.name for s in store.get_run("run-1").steps] == ["Sentinel"]


def test_databricks_uri_without_credentials_is_disabled() -> None:
    assert not MlflowTracer("databricks", databricks_ready=False).enabled
    assert MlflowTracer("databricks", databricks_ready=True).enabled
