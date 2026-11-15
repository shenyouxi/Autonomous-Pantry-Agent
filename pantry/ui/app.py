import json
from pathlib import Path
import sys

import streamlit as st

# ``streamlit run pantry/ui/app.py`` executes with pantry/ui as sys.path[0]
# on newer Streamlit releases. Add the repository root so package imports work
# without requiring contributors to set PYTHONPATH manually.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pantry.config import get_settings
from pantry.store.factory import make_store
from pantry.tracing import trace_id_for_run
from pantry.ui.live import command_status_row, enqueue_ui_command


DEMO_SCRIPT_PATH = REPO_ROOT / "seed_data" / "demo_script.json"


def remember_command(command_id: str) -> None:
    command_ids = st.session_state.setdefault("command_ids", [])
    if command_id not in command_ids:
        command_ids.insert(0, command_id)
    del command_ids[20:]


def send(command_type: str, payload: dict | None = None) -> None:
    command = enqueue_ui_command(store, command_type, payload)
    remember_command(command.command_id)
    st.toast(f"Queued {command.type}: {command.command_id}")


with DEMO_SCRIPT_PATH.open(encoding="utf-8") as file:
    demo = json.load(file)

st.set_page_config(page_title="Pantry Agent Demo", page_icon="🥬", layout="wide")
st.title("Pantry Agent")

settings = get_settings()
try:
    store = make_store(None, settings)
except Exception as exc:
    st.error(f"Could not connect to the {settings.store_backend} Store: {exc}")
    st.stop()

today = store.get_today()
runs = store.list_runs(limit=50)
pending_runs = [
    run
    for run in runs
    if run.state == "PENDING_CONFIRMATION" or run.pending_confirmation is not None
]
auto_run_state = store.get_auto_run()

with st.sidebar:
    st.header("Demo controls")
    st.caption(f"Backend: {settings.store_backend}")
    st.caption(f"Virtual date: {today}")
    st.caption(f"Auto-run: {auto_run_state.mode} ({auto_run_state.days_completed}/{auto_run_state.max_days} days)")
    if st.button("Refresh live data", width="stretch"):
        st.rerun()

    if auto_run_state.mode == "running":
        if st.button("Pause auto-run", width="stretch"):
            send("PAUSE_AUTO_RUN")
    elif st.button("Start 3-day auto-run", width="stretch"):
        send("START_AUTO_RUN", {"max_days": 3})

    if auto_run_state.pause_reason:
        st.warning(f"Auto-run paused: {auto_run_state.pause_reason}")

    st.divider()
    st.subheader("Clock")
    if pending_runs:
        st.warning("Resolve the pending confirmation before advancing the clock.")
    if st.button("Advance one day", disabled=bool(pending_runs), width="stretch"):
        send("ADVANCE_CLOCK", {"days": 1})

    if settings.store_backend == "json":
        st.warning(
            "JSON mode is single-process. Use Mongo for a separate worker and UI, "
            "or run the offline flow in one process."
        )

day_index = max((today - settings.demo_start_date).days, 0)
st.header(f"Demo Day {day_index + 1} · Live runs")
st.caption("All actions below use persisted Store data; scripted expectations remain available for comparison.")

items = store.list_items()
inventory_rows = []
for item in items:
    days_left = None if item.expiry_date is None else (item.expiry_date - today).days
    inventory_rows.append(
        {
            "item": item.item_name,
            "available": item.quantity - item.reserved,
            "reserved": item.reserved,
            "unit": item.unit,
            "days_left": days_left,
            "date_label": item.date_label,
            "packaging_unverified": item.packaging_unverified,
        }
    )

inventory_tab, runs_tab, script_tab, commands_tab = st.tabs(
    ["Live inventory", "Runs & decisions", "Script reference", "Command status"]
)

with inventory_tab:
    if inventory_rows:
        st.dataframe(inventory_rows, hide_index=True, width="stretch")
    else:
        st.info("No inventory is loaded. Run the seed step first.")

with runs_tab:
    if not runs:
        st.info("No live runs yet. Start the worker after the demo Store has been seeded and reset.")
    else:
        selected_run_id = st.selectbox(
            "Run",
            options=[run.run_id for run in runs],
            format_func=lambda run_id: next(
                f"{run.item_name} · {run.state} · {run.run_id}"
                for run in runs
                if run.run_id == run_id
            ),
        )
        run = next(run for run in runs if run.run_id == selected_run_id)
        trace = store.get_trace_by_run(run.run_id)
        trace_id = trace.trace_id if trace else trace_id_for_run(run.run_id)

        id_col, trace_col, state_col = st.columns(3)
        id_col.metric("Run ID", run.run_id)
        trace_col.metric("Trace ID", trace_id)
        state_col.metric("State", run.state)

        confirmation = run.pending_confirmation
        if confirmation is not None or run.state == "PENDING_CONFIRMATION":
            st.warning("Human confirmation required")
            if confirmation is not None:
                st.write(confirmation.evidence)
                st.caption(f"Safety rule: {confirmation.rule}")
                for url in confirmation.citation_urls:
                    st.markdown(f"- [{url}]({url})")

            reject_col, approve_col = st.columns(2)
            if reject_col.button("Reject plan", type="primary", width="stretch"):
                send("REJECT", {"run_id": run.run_id})
            if confirmation is not None and confirmation.rule != "allergen":
                if approve_col.button("Approve after review", width="stretch"):
                    send("APPROVE", {"run_id": run.run_id})
            else:
                approve_col.info("Allergen risk cannot be approved in the demo UI.")

        st.subheader("Timeline")
        if run.steps:
            st.dataframe(
                [
                    {
                        "time": step.ts,
                        "step": step.name,
                        "status": step.status,
                        "detail": json.dumps(step.detail, ensure_ascii=False),
                    }
                    for step in run.steps
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("This run has no recorded steps yet.")

        if trace is None:
            st.info("The decision trace has not been written yet.")
        else:
            st.subheader("Decision trace")
            st.write(f"Outcome: **{trace.final_outcome}** · Risk: **{trace.risk_status}**")

            if trace.retrieved_traces:
                st.write("Retrieved prior decisions")
                st.dataframe(
                    [item.model_dump(mode="json") for item in trace.retrieved_traces],
                    hide_index=True,
                    width="stretch",
                )

            if trace.resolver_actions:
                st.write("Resolver actions and sources")
                for index, action in enumerate(trace.resolver_actions, start=1):
                    with st.expander(f"{index}. {action.tool}: {action.query}"):
                        st.write(action.answer or "No answer recorded")
                        if action.degraded:
                            st.warning(action.degraded_reason or "Resolver used a degraded fallback.")
                        for source in action.sources:
                            st.markdown(f"- [{source['title']}]({source['url']}): {source['content']}")

            feedback = store.list_feedback(trace.trace_id)
            st.write("Feedback status")
            if feedback:
                st.dataframe(
                    [item.model_dump(mode="json") for item in feedback],
                    hide_index=True,
                    width="stretch",
                )
            else:
                st.caption("No feedback recorded yet.")

            feedback_col, reject_feedback_col = st.columns(2)
            if feedback_col.button("Mark result helpful", width="stretch"):
                send(
                    "FEEDBACK",
                    {"trace_id": trace.trace_id, "outcome": "accepted", "source": "user"},
                )
            if reject_feedback_col.button("Mark result unhelpful", width="stretch"):
                send(
                    "FEEDBACK",
                    {"trace_id": trace.trace_id, "outcome": "rejected", "source": "user"},
                )

with script_tab:
    round_number = st.selectbox(
        "Scripted round",
        options=[item["round_number"] for item in demo["rounds"]],
    )
    round_data = next(item for item in demo["rounds"] if item["round_number"] == round_number)
    st.caption("Reference only — these are expected results, not recorded agent actions.")
    for step_number, step in enumerate(round_data["expected_steps"], start=1):
        st.write(f"{step_number}. {step}")

with commands_tab:
    command_ids = st.session_state.get("command_ids", [])
    command_rows = []
    for command_id in command_ids:
        command = store.get_command(command_id)
        if command is not None:
            command_rows.append(command_status_row(command))
    if command_rows:
        st.dataframe(command_rows, hide_index=True, width="stretch")
        for row in command_rows:
            if row["status"] == "failed":
                st.error(f"{row['type']} failed: {row['error']}")
    else:
        st.info("Commands sent from this browser session will appear here.")
