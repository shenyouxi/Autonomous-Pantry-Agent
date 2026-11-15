# Demo Runbook

One-page checklist for running the three-round Pantry Agent demo. Run every command from the repo root.

## 1. Prerequisites

- Python virtual environment with dependencies installed:
  ```bash
  python -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  ```
- Copy `.env.example` to `.env` and fill it in. **Never paste secret values into chat, docs or commits.**
  - MongoDB: `MONGODB_URI`, `MONGODB_DB`, `STORE_BACKEND=mongo` (use `json` to run offline)
  - LLM: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `EMBED_PROVIDER=openai`, `EMBED_MODEL`
  - Search: `LINKUP_API_KEY`
  - Tracing (optional): `MLFLOW_TRACKING_URI`, `DATABRICKS_HOST`, `DATABRICKS_TOKEN`
  - Demo: `DEMO_START_DATE=2026-11-14`, `TICK_SECONDS`, `CACHE_FIRST`

## 2. Before the demo (in order)

1. **Preflight**: checks env var names, Mongo ping, embedding dimension (1536), chat JSON mode and search keys. It prints PASS/FAIL with a fallback for each check and exits non-zero if any check fails.
   ```bash
   python scripts/preflight.py
   ```
2. **Seed** (first time only, or after changing `seed_data/`): writes the pantry, profile and seed traces, then waits up to 120 s for the Atlas vector index.
   ```bash
   python scripts/seed.py          # same as: python -m pantry.seed
   ```
3. **Reset to day 1**: safe to rerun. It keeps `search_cache`, `plan_cache` and seed traces, and clears runs, bus events, commands, ledger, feedback and the shopping list.
   ```bash
   python scripts/reset_demo.py    # add --backend json to override STORE_BACKEND
   ```
4. **Verify invariants**: `reset_demo.py` must end with `PASS inventory invariants hold`. If it prints `FAIL invariant violations after reset`, or `warning: no pantry baseline found`, run step 2 and then step 3 again.
5. **Verify auto-run is off**: reset puts the stored auto-run state back to its default (`mode='off'`, `days_completed=0`). Check it before going live:
   ```bash
   python -c "from pantry.config import get_settings; from pantry.store.factory import make_store; print(make_store(None, get_settings()).get_auto_run())"
   ```
   Expect `mode='off'`. If it shows `running` or `paused`, rerun step 3.

## 3. Start the system

- **Worker** (consumes commands and runs the agent):
  ```bash
  STORE_BACKEND=mongo python -m pantry.agents.worker
  ```
  The current offline `FakeLLM` has no registered `PlannerOutput`, so do not use the worker for the scripted demo until B adds that registration or a cached planner path.
- **Operator commands** (enqueue one worker command; `--list` shows the auto-run state, runs and their ids):
  ```bash
  python scripts/send_command.py ADVANCE_CLOCK | APPROVE --run-id ID | REJECT --run-id ID | UNDO_RUN --run-id ID | FEEDBACK --trace-id ID --outcome accepted|rejected
  python scripts/send_command.py START_AUTO_RUN [--max-days N]
  python scripts/send_command.py PAUSE_AUTO_RUN
  python scripts/send_command.py --list
  ```
  Always run scripts/reset_demo.py before each rehearsal: START_AUTO_RUN does not reset days_completed, so a finished auto-run turns itself off again immediately.
- **UI**:
  ```bash
  streamlit run pantry/ui/app.py
  ```
  The UI reads live inventory, runs, traces, resolver sources and feedback from the selected Store. It writes `ADVANCE_CLOCK`, `APPROVE`, `REJECT` and `FEEDBACK` commands and displays their pending/done/failed status. `seed_data/demo_script.json` remains visible as a reference tab.

  **Offline blocker:** `JsonStore` is single-process and keeps an in-memory snapshot. A separately started worker will not reliably observe commands written by the Streamlit process. Use Mongo when running worker and UI as separate processes, or keep the offline E2E flow in one process until a shared JSON runtime is implemented.

## 4. Demo flow (from `seed_data/demo_script.json`)

Script settings: `auto_run: true` and `auto_accept_non_risk: true`. These are separate from the stored auto-run state, which is off after reset. How it gets switched on for the demo is a TODO: confirm with B/C. A trigger fires once when an item first enters the 2-day urgency window. Each new day is reached with an **ADVANCE_CLOCK** command.

| Day | Trigger | Operator action | Audience should see |
| --- | --- | --- | --- |
| Day 1 (2026-11-14, day_index 0) | `EXPIRY_IMMINENT`: milk, 1 day to use-by | None (0 clicks) | Planner proposes creamy garlic pasta for 2 (r5). Resolver finds 100 ml of double cream missing, and Linkup returns a cited substitute (75 ml milk + 25 g butter). The rule gate passes and the plan is **auto-accepted**. MEAL_COMPLETED applies the inventory changes (for example, milk 700 → 625 ml). An auto-accept feedback is written. |
| Day 2 (2026-11-15, day_index 1) | ADVANCE_CLOCK, then `EXPIRY_IMMINENT`: eggs, 2 days | ADVANCE_CLOCK only | Planner proposes creamy carbonara for 4 (r6). **Memory retrieval shows the Round 1 trace in the UI.** The cream substitution is reused at a smaller scale (56.25 ml milk + 18.75 g butter). The plan is auto-accepted and eggs go 6 → 3. |
| Day 3 (2026-11-16, day_index 2) | ADVANCE_CLOCK, then `EXPIRY_IMMINENT`: baby spinach, 2 days | ADVANCE_CLOCK, then **REJECT** (1 click) | Resolver surfaces walnut pesto (r4). The allergen gate flags guest u3 (peanuts, tree nuts), the run pauses at `pending_confirmation`, and a risk card shows the source and the affected guest. There is no generic Accept button. After REJECT, the state is `rejected`, the inventory deduction is 0, and the rejected feedback is searchable. |

Commands: `ADVANCE_CLOCK`, `APPROVE`, `REJECT` and `FEEDBACK` are defined in `pantry/models.py` (`CommandType`). The UI writes them through `Store.enqueue_command()` and shows feedback or inventory results only after the worker marks a command `done`. Failed commands remain visible with their error message.

## 5. If something goes wrong

- **Reset at any point**: `python scripts/reset_demo.py`, then restart from Day 1.
- **Atlas vector index status** (index `trace_vector_index` on `decision_traces`):
  ```bash
  python -c "from dotenv import load_dotenv; load_dotenv('.env'); from pantry.store.mongo_store import MongoStore; s=MongoStore(); print(s.vector_index_status()); s.close()"
  ```
  Look for `queryable: True`. While the index is not queryable, `search_traces` uses the local fallback. Rerunning `python scripts/seed.py` recreates the index and waits for it again.
- **MLflow / Databricks down**: tracing is fail-safe. It logs one warning, disables MLflow for the process, and keeps writing to the Store. **The demo continues.**
- **Linkup down**: set `CACHE_FIRST=true` to use the search and plan caches, which `reset_demo` keeps. Preflight lists FakeLinkupClient or the search cache as the offline fallback.
- **Mongo unreachable**: set `STORE_BACKEND=json`, then reseed and reset with `--backend json`.
- **LLM down**: FakeLLM or cached plans with `CACHE_FIRST=true`. Seeding offline: `python scripts/seed.py --fake-llm` (similarity results will not be meaningful).

## 6. After the hackathon

- In Atlas Network Access, remove the `0.0.0.0/0` IP allowlist entry.
- Delete the Databricks personal access token (`DATABRICKS_TOKEN`).
- Rotate the LLM, Linkup and MongoDB credentials, and clear them from your local `.env`.
