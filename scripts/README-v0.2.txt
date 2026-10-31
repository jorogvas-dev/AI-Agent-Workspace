TASK CONTROLLER v0.2 -- LIVE KNOWLEDGE RETRIEVAL
================================================

This is an OPTIONAL upgrade to v0.1. It does not alter your running
containers or original task_controller.py.

FILES
-----
- task_controller_v02.py
- job_offer_live_plan.json

INSTALL
-------
Copy both files to E:\AI-Agent-Workspace\scripts\

Windows PowerShell:
  py "E:\AI-Agent-Workspace\scripts\task_controller_v02.py" run "E:\AI-Agent-Workspace\scripts\job_offer_live_plan.json"

Inspect output:
  Get-Content "E:\AI-Agent-Workspace\output\job-offer-live-summary.md"

Inspect step audit:
  py "E:\AI-Agent-Workspace\scripts\task_controller_v02.py" status "E:\AI-Agent-Workspace\scripts\job_offer_live_plan.json"

PRECONDITIONS
-------------
- Docker Desktop running.
- Production container named "knowledge-service" is running and healthy.
- Python executable is available inside knowledge-service (as in the current container).
- Server listens inside its container on 127.0.0.1:8002 and exposes POST /search
  with JSON {"query": "...", "limit": 10}.
- The container already has the KNOWLEDGE_TOKEN environment variable.
- sample.pdf has been indexed in the Chroma knowledge collection.
- Windows user can run docker exec on this container.
- Python 3 on Windows and output/ and temp/ workspace directories exist.

HOW IT WORKS
------------
- For each lookup_number step, invoke Python inside knowledge-service via
  docker exec. Inside the container, POST /search using its existing token.
- Require a matching source name and a bounded, explicitly reviewed regular
  expression in the plan to extract one numeric fact.
- Reject absent/conflicting facts and halt; do not silently assume values.
- Calculate three totals with Decimal arithmetic.
- Write via atomic file replacement, then verify SHA-256 digest.
- Record state in E:\AI-Agent-Workspace\temp\task-state-*.json.

NOTES
-----
- The plan's regexes are deliberately tailored to the sample.pdf form.
  This is a focused integration test, NOT general-purpose fact extraction.
- This package was tested against local mock service responses, including
  8-step calculation, writing, verification. Actual service connectivity and
  document retrieval cannot be tested outside your computer.
- No host ports are opened and no token values are stored in these files.
- A trusted human should review plans, especially regular expressions and
  overwrite targets, before execution. Do not expose this runner to arbitrary
  model-generated unreviewed JSON.
- The v0.2 script is separate from v0.1, preserving an easy rollback.
- No auto-retry, model planning, or arbitrary code execution has been added.
