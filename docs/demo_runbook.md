# Demo Runbook

One-page checklist for the three-day Pantry Agent demo. Run every command from the repo root.

## 1. Setup

- **The demo runs on A's laptop.** The worker and the Streamlit UI run on the same machine, against the same MongoDB Atlas database, using one `.env`.
- **B and C develop against `JsonStore` and the fakes** (`STORE_BACKEND=json`, FakeLLM, FakeLinkupClient). They do not need the Atlas credentials.
- **Credentials are never committed** and are only shared privately. Copy `.env.example` to `.env` and fill it in locally; never paste secret values into chat, docs or commits.
- Install: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Once per machine (or after changing `seed_data/`): `python scripts/preflight.py` (must be all PASS), then `python scripts/seed.py` (waits up to 120 s for the Atlas vector index).

## 2. Start sequence

**Always run `reset_demo.py` before each rehearsal**: `START_AUTO_RUN` does not reset completed days, so a finished auto-run turns itself off again immediately.

- **Terminal 1 (worker)**
  ```bash
  python scripts/reset_demo.py          # must end with "PASS inventory invariants hold"
  python -m pantry.agents.worker
  ```
- **Terminal 2 (UI)**
  ```bash
  streamlit run pantry/ui/app.py
  ```
  The UI shows live data from the same database: inventory, runs (timeline, decision trace, cited sources, feedback) and command status are read from the persisted run/trace data, with no scripted preview. Sidebar buttons: Start 3-day auto-run / Pause auto-run and Advance one day (disabled while a confirmation is pending). A `GATED` or `PENDING_CONFIRMATION` run shows a warning, a failed step shows an error banner, and failed commands show their error in the Command status tab. A pending confirmation offers Reject plan; Approve after review appears only for non-allergen rules.
- **Terminal 3 (operator)**
  ```bash
  python scripts/send_command.py ADVANCE_CLOCK --wait 60
  python scripts/send_command.py REJECT --run-id ID --wait 60     # also APPROVE, UNDO_RUN
  python scripts/send_command.py FEEDBACK --trace-id ID --outcome accepted|rejected
  python scripts/send_command.py START_AUTO_RUN [--max-days N]
  python scripts/send_command.py PAUSE_AUTO_RUN
  python scripts/send_command.py --list                           # auto-run state, runs and run ids
  ```
  `ADVANCE_CLOCK` needs `--wait SECONDS` (for example `--wait 60`) so the command blocks until the worker marks it done or failed. Without `--wait` the script only enqueues it.

**Demo flow** (`seed_data/demo_script.json`): Day 1 milk expiry → creamy garlic pasta, cited cream substitute, auto-accepted. Day 2 (`ADVANCE_CLOCK`) eggs → carbonara, reuses the Day 1 trace from memory, auto-accepted. Day 3 (`ADVANCE_CLOCK`) spinach → walnut pesto, allergen gate pauses at `pending_confirmation`, operator sends `REJECT`, inventory deduction is 0.

## 3. Prewarm

Run before the demo, once the live services pass preflight:

```bash
python scripts/prewarm.py          # live: Atlas + LLM + Linkup
python scripts/prewarm.py --fake   # dry run: FakeLLM, FakeLinkupClient, in-memory JsonStore
```

`prewarm.py` resets, runs the three days live with `CACHE_FIRST=false` to fill `search_cache` and `plan_cache`, resets again, then replays the three days offline with every LLM and Linkup call blocked. **It fails on any cache miss** and exits non-zero. It ends with a final reset.

After a successful prewarm, **set `CACHE_FIRST=true` in `.env` for the demo**, so a slow LLM or Linkup does not matter. `reset_demo.py` keeps both caches.

## 4. Fallback

- **LLM or Linkup slow or down**: set `CACHE_FIRST=true` in `.env` and restart the worker (Ctrl+C in terminal 1, then `python -m pantry.agents.worker`).
- **Worker crashes**: restart it. The intended rule is that an unfinished run is re-executed from the start with the same idempotency key. **This resume rule is not implemented yet: check with B.** In the current worker, a restarted dispatch finds the existing run by its idempotency key and does not execute it again. If a run is stuck, run `python scripts/reset_demo.py`, restart the worker and start again from Day 1.
- **Mongo unreachable**: set `STORE_BACKEND=json`, then reseed and reset with `--backend json`.
- **MLflow / Databricks down**: tracing is fail-safe (one warning, then MLflow is disabled). The demo continues.
- **Vector index not queryable**: `search_traces` uses the local fallback. Rerun `python scripts/seed.py` to recreate it.

## 5. Known behaviour

The live planner may choose a different dish than `demo_script.json`. The narrative follows the cached run, so use `CACHE_FIRST=true` after prewarm to make the demo match the script.

## 6. After the hackathon

Remove the `0.0.0.0/0` Atlas IP allowlist entry, delete the Databricks token, rotate the LLM, Linkup and MongoDB credentials, and clear them from your local `.env`.
