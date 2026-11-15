# Demo Runbook

Operational checklist for the three-day Pantry Agent demo. Run every command from the repo root (`~/Hack_Agent`).

## 1. Setup

- **Plan A: the demo runs on A's laptop only.** The worker and the Streamlit UI run as separate processes against the same MongoDB Atlas database, using one `.env`.
- **B and C use `JsonStore` and the fakes** (FakeLLM, FakeLinkupClient) for their own work. They do not need the Atlas credentials.
- **The plan and search caches live in Atlas, not in git.** The demo must run on this machine, against this database, with this `.env`.
- **`.env` must contain `CACHE_FIRST=true` for the demo.** Use `CACHE_FIRST=false` only for prewarm (section 4).
- **Credentials are never written in this file**, in chat or in commits. They live only in the local `.env`.

## 2. Start sequence (3 terminals)

**Always reset before every rehearsal and before the real demo, and restart the worker after every reset.**

- **Terminal 1 (worker, leave running)**
  ```bash
  cd ~/Hack_Agent && source .venv/bin/activate && python scripts/reset_demo.py
  STORE_BACKEND=mongo python -m pantry.agents.worker
  ```
  `reset_demo.py` must end with `PASS inventory invariants hold`.
- **Terminal 2 (operator)**
  ```bash
  python scripts/send_command.py --list                     # expect mode=off, today=2026-11-14, runs=0
  python scripts/send_command.py START_AUTO_RUN --wait 60   # or the UI button "Start 3-day auto-run"
  python scripts/send_command.py --list                     # watch progress
  ```
- **Terminal 3 (UI)**
  ```bash
  streamlit run pantry/ui/app.py --server.headless true
  ```
  Open the Local URL it prints and click **Refresh live data**.

## 3. Demo narrative (cached run)

Read the values from the UI; these are what the cache replays:

- **Day 1**: milk → *Creamy Broccoli and Ham Penne Bake-Style Pasta*.
- **Day 2**: milk → *Spaghetti alla carbonara with milk and spinach*, and eggs → *Creamy Spinach and Pancetta Spaghetti Carbonara*.
- **Day 3**: baby spinach → *Lemon Yogurt Tagliatelle with Spinach and Mushrooms*.

The three days finish automatically in seconds, every run is `RECORDED` and there are no gaps.

**Memory**: later days retrieve earlier decision traces by vector search (Timeline rows `Embedding` / `vector_search`).

This cached run has **no Resolver/Linkup step and no allergen gate**. Do not narrate them.

## 3b. Linkup stage demo (optional, live)

**Purpose**: the cached run has no gap, so it never calls Linkup. This standalone script shows the Resolver live: a missing ingredient → real Linkup search → LLM extraction → pantry check, with cited source links. It writes no runs, traces, events or inventory; it only adds its own `search_cache` entry.

**When**: run it with the worker **stopped** (or before it is started), right after `python scripts/reset_demo.py`, so the pantry is the clean Day 1 stock. Do not run it while the worker is running: `reset_demo.py` makes a running worker start the Day 1 run immediately, which reserves stock, and the script then reads inconsistent quantities. Alternatively, run it after the three days have finished and accept the depleted stock. Afterwards, restart the worker and run `python scripts/reset_demo.py` again before the auto-run.

- **Terminal 4 (Linkup stage, optional)**
  ```bash
  python scripts/linkup_demo.py                                          # default: eggs, 2 pcs
  python scripts/linkup_demo.py --item "heavy cream" --amount 200 --unit ml --no-cache   # stage version
  ```
  It needs network and takes from a few seconds to roughly fifteen seconds, because Linkup and the LLM are called live. Without `--no-cache`, a repeated query is served from `search_cache` and prints "Linkup and LLM not called", so use `--no-cache` when showing it live.

**What to say**: the agent found a sourced substitute. If the substitute cannot be covered from the pantry, or the units cannot be converted (for example cups to grams), the system refuses to guess and a human must confirm.

**If it fails**: if Linkup or the LLM is unavailable, the script exits non-zero instead of faking data. Skip this step and show the cited sources from the README screenshot instead.

## 4. Prewarm (only if the cache must be rebuilt)

1. Stop the worker and the UI.
2. Set `CACHE_FIRST=false` in `.env`.
3. Run:
   ```bash
   python scripts/prewarm.py
   ```
   Expect `PASS offline replay`.
4. Set `CACHE_FIRST=true` in `.env` again, then follow section 2.

The LLM is deterministic for identical input, so rerunning prewarm gives the same plans. **Any change to `rules.py`, the planner prompt, the snapshot text, the seed data or the Resolver prompt invalidates the cache keys** and requires a new prewarm.

## 5. If something goes wrong

- **Reset and restart from Day 1.** There is no resume rule: run `python scripts/reset_demo.py`, restart the worker, start the auto-run again.
- **A command fails**: check the UI **Command status** tab for the error.
- **A run shows `GATED`**: use **Reject plan**. Do not use **Approve after review** when the substitute is not in the pantry; it fails by design.
- **Cache miss**: the worker would call the live LLM and the plans may differ. Reset, check that `.env` has `CACHE_FIRST=true`, and make sure the worker was restarted after any `.env` change.
- **Atlas vector index status** (index `trace_vector_index` on `decision_traces`):
  ```bash
  python -c "from dotenv import load_dotenv; load_dotenv('.env'); from pantry.store.mongo_store import MongoStore; s=MongoStore(); print(s.vector_index_status()); s.close()"
  ```
  Look for `queryable: True`. While the index is not queryable, `search_traces` uses the local fallback. Rerunning `python scripts/seed.py` recreates the index and waits for it again.

## 6. After the hackathon

- Remove the `0.0.0.0/0` Atlas IP allowlist entry.
- Delete the Databricks token.
- Rotate the LLM, Linkup and MongoDB credentials.
- Clear the local `.env`.
