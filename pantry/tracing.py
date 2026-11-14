# The single tracing instrumentation: one trace_step call writes runs.steps, optional checkpoints and an MLflow span.
# MLflow is optional and fail-safe; save_decision_trace upserts the decision Trace with the run's steps.
"""Tracing for agent runs.

Public API
----------
trace_step(store, run_id, name, *, tracer=None, **detail) -> ContextManager[StepHandle]
    Context manager around one agent step. On exit it appends exactly one RunStep
    {ts, name, status, detail} to the run (Store.append_step, or Store.save_checkpoint when the body
    called handle.checkpoint(...)) and closes an MLflow span with the same name. Nested trace_step
    calls produce nested MLflow spans (Sentinel -> Planner[vector_search, llm] -> ...); in runs.steps
    each step is appended when it finishes, with ts set to its start time. An exception in the body
    is recorded as a "failed" step (detail["error"] = "ExcType: message") and re-raised.

StepHandle
    .set(**detail)            merge keys into the step detail (keep it short; long strings are cut)
    .degrade(reason)          mark the step "degraded" with detail["degraded_reason"] (fallback used)
    .skip(reason)             mark the step "skipped"
    .checkpoint(state, data)  on success, write the step via Store.save_checkpoint(run_id, state, data, step)
                              so the state transition, checkpoint and step are one atomic write

trace_id_for_run(run_id) -> str
    Deterministic trace_id for a run, so a resumed run upserts the same decision trace.

save_decision_trace(store, trace, *, tracer=None) -> Trace
    Copies the run's current steps into trace.steps and upserts it with Store.save_trace. Call it at
    GATED (final_outcome="pending_confirmation") so the confirmation card has a trace_id, and again
    at RECORDED; every call overwrites the same trace_id.

MlflowTracer(tracking_uri, experiment_name=MLFLOW_EXPERIMENT_NAME)
get_tracer() / set_tracer(tracer)
    Process-wide tracer built from Settings. Disabled when MLFLOW_TRACKING_URI is unset, or when the
    URI is Databricks and DATABRICKS_HOST/DATABRICKS_TOKEN are missing (MLflow reads both from the
    environment). Any MLflow failure logs one warning and disables MLflow for the process; Store writes
    always happen regardless.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from pantry.config import MLFLOW_EXPERIMENT_NAME, get_settings
from pantry.models import RunState, RunStep, StepStatus, Trace, utc_now
from pantry.store.base import Store

logger = logging.getLogger(__name__)

MAX_DETAIL_CHARS: int = 300  # long strings in step detail are cut to keep the UI timeline short
ERROR_STATUS: StepStatus = "failed"  # models.StepStatus has no "error"; "failed" is the error status


def _short(value: Any) -> Any:
    """Cut long strings so step details stay short."""
    if isinstance(value, str) and len(value) > MAX_DETAIL_CHARS:
        return value[: MAX_DETAIL_CHARS - 3] + "..."
    return value


def trace_id_for_run(run_id: str) -> str:
    """Return the deterministic trace_id for a run."""
    return f"trace:{run_id}"


# ---------------------------------------------------------------------------
# MLflow (optional, never raises)
# ---------------------------------------------------------------------------


class MlflowTracer:
    """Thin fail-safe wrapper over MLflow spans; every MLflow error disables it with one warning."""

    def __init__(self, tracking_uri: str, experiment_name: str = MLFLOW_EXPERIMENT_NAME,
                 *, databricks_ready: bool = True, redact: tuple[str, ...] = ()) -> None:
        self._tracking_uri = tracking_uri
        self._experiment_name = experiment_name
        self._redact = tuple(r for r in redact if r)
        self._configured = False
        self._lock = threading.Lock()
        self._enabled = bool(tracking_uri)
        if self._enabled and tracking_uri.startswith("databricks") and not databricks_ready:
            logger.warning("MLflow tracing disabled: Databricks URI set but DATABRICKS_HOST or DATABRICKS_TOKEN is missing")
            self._enabled = False

    @classmethod
    def from_settings(cls) -> MlflowTracer:
        """Build the tracer from Settings; credentials stay in the environment and are never logged."""
        settings = get_settings()
        return cls(
            settings.mlflow_tracking_uri,
            MLFLOW_EXPERIMENT_NAME,
            databricks_ready=bool(settings.databricks_host and settings.databricks_token),
            redact=(settings.databricks_token,),
        )

    @property
    def enabled(self) -> bool:
        """True while MLflow spans are being written."""
        return self._enabled

    def _disable(self, action: str, exc: BaseException) -> None:
        """Log one warning (secrets redacted) and stop using MLflow for this process."""
        message = f"{type(exc).__name__}: {exc}"
        for secret in self._redact:
            message = message.replace(secret, "***")
        if self._enabled:
            logger.warning("MLflow tracing disabled after %s failed: %s", action, _short(message))
        self._enabled = False

    def _ensure_configured(self) -> bool:
        """Set the tracking URI and experiment once, lazily, so imports and tests make no network calls."""
        if not self._enabled:
            return False
        if self._configured:
            return True
        with self._lock:
            if not self._configured:
                try:
                    import mlflow

                    mlflow.set_tracking_uri(self._tracking_uri)
                    mlflow.set_experiment(self._experiment_name)
                    self._configured = True
                except Exception as exc:  # noqa: BLE001 - MLflow must never break the agent
                    self._disable("setup", exc)
        return self._enabled

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any]) -> Iterator[Any]:
        """Open an MLflow span around the body; yields the live span or None when disabled or failing.
        Exceptions from the body propagate unchanged; MLflow errors are swallowed."""
        cm = None
        live = None
        if self._ensure_configured():
            try:
                import mlflow

                cm = mlflow.start_span(name=name)
                live = cm.__enter__()
                live.set_attributes(attributes)
            except Exception as exc:  # noqa: BLE001
                self._disable("start_span", exc)
                cm = None
                live = None
        exc_info: tuple[Any, Any, Any] = (None, None, None)
        try:
            yield live
        except BaseException as body_exc:
            exc_info = (type(body_exc), body_exc, body_exc.__traceback__)
            raise
        finally:
            if cm is not None:
                try:
                    cm.__exit__(*exc_info)
                except Exception as exc:  # noqa: BLE001
                    self._disable("end_span", exc)

    def finish(self, live: Any, status: StepStatus, detail: dict[str, Any]) -> None:
        """Attach the step outcome to a live span; no-op when the span is None."""
        if live is None or not self._enabled:
            return
        try:
            live.set_attributes({"status": status, "detail": json.dumps(detail, default=str)})
        except Exception as exc:  # noqa: BLE001
            self._disable("set_attributes", exc)


_tracer: MlflowTracer | None = None
_tracer_lock = threading.Lock()


def get_tracer() -> MlflowTracer:
    """Return the process-wide tracer, building it from Settings on first use."""
    global _tracer
    if _tracer is None:
        with _tracer_lock:
            if _tracer is None:
                try:
                    _tracer = MlflowTracer.from_settings()
                except Exception as exc:  # noqa: BLE001 - bad config must not break the agent
                    logger.warning("MLflow tracing disabled: settings could not be loaded (%s)", type(exc).__name__)
                    _tracer = MlflowTracer("")
    return _tracer


def set_tracer(tracer: MlflowTracer | None) -> None:
    """Replace the process-wide tracer (None rebuilds it from Settings on next use)."""
    global _tracer
    _tracer = tracer


# ---------------------------------------------------------------------------
# trace_step
# ---------------------------------------------------------------------------


class StepHandle:
    """Mutable view of the step being recorded, yielded by trace_step."""

    def __init__(self, name: str, detail: dict[str, Any]) -> None:
        self.name = name
        self.ts: datetime = utc_now()
        self.status: StepStatus = "ok"
        self.detail: dict[str, Any] = {k: _short(v) for k, v in detail.items()}
        self.span: Any = None  # the live MLflow span, or None
        self._checkpoint: tuple[RunState, dict[str, object]] | None = None

    def set(self, **detail: Any) -> None:
        """Merge keys into the step detail."""
        self.detail.update({k: _short(v) for k, v in detail.items()})

    def degrade(self, reason: str) -> None:
        """Mark the step degraded because a fallback was used."""
        self.status = "degraded"
        self.detail["degraded"] = True
        self.detail["degraded_reason"] = _short(reason)

    def skip(self, reason: str) -> None:
        """Mark the step skipped."""
        self.status = "skipped"
        self.detail["skipped_reason"] = _short(reason)

    def checkpoint(self, state: RunState, data: dict[str, object]) -> None:
        """Ask trace_step to write this step together with a state transition and checkpoint."""
        self._checkpoint = (state, data)

    def to_step(self) -> RunStep:
        """Build the RunStep stored in runs.steps."""
        return RunStep(ts=self.ts, name=self.name, status=self.status, detail=dict(self.detail))


@contextmanager
def trace_step(store: Store, run_id: str, name: str, *, tracer: MlflowTracer | None = None,
               **detail: Any) -> Iterator[StepHandle]:
    """Record one agent step in runs.steps (and checkpoints when requested) plus an MLflow span."""
    tracer = tracer if tracer is not None else get_tracer()
    handle = StepHandle(name, detail)
    with tracer.span(name, {"run_id": run_id, "step": name}) as live:
        handle.span = live
        try:
            yield handle
        except BaseException as exc:
            handle.status = ERROR_STATUS
            handle.detail["error"] = _short(f"{type(exc).__name__}: {exc}")
            handle._checkpoint = None
            tracer.finish(live, handle.status, handle.detail)
            try:
                store.append_step(run_id, handle.to_step())
            except Exception:  # keep the original exception; the store error is only logged
                logger.exception("Could not record failed step %s for run %s", name, run_id)
            raise
        tracer.finish(live, handle.status, handle.detail)
        if handle._checkpoint is not None:
            state, data = handle._checkpoint
            store.save_checkpoint(run_id, state, data, handle.to_step())
        else:
            store.append_step(run_id, handle.to_step())


# ---------------------------------------------------------------------------
# Decision trace
# ---------------------------------------------------------------------------


def save_decision_trace(store: Store, trace: Trace, *, tracer: MlflowTracer | None = None) -> Trace:
    """Copy the run's steps into the trace and upsert it; safe to call at GATED and again at RECORDED."""
    if trace.run_id:
        run = store.get_run(trace.run_id)
        if run is not None:
            trace = trace.model_copy(update={"steps": list(run.steps)})
    stored = store.save_trace(trace)
    tracer = tracer if tracer is not None else get_tracer()
    if tracer.enabled:
        try:
            import mlflow

            mlflow.update_current_trace(tags={"decision_trace_id": stored.trace_id,
                                              "final_outcome": stored.final_outcome})
        except Exception:  # noqa: BLE001 - outside an active span this is expected to fail; never fatal
            pass
    return stored
