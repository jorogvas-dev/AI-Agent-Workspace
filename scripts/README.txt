TASK CONTROLLER v0.1 (LOCAL MVP)
=================================

1. Copy task_controller.py and job_offer_plan.json to:
   E:\AI-Agent-Workspace\scripts\

2. In PowerShell run:
   py "E:\AI-Agent-Workspace\scripts\task_controller.py" run "E:\AI-Agent-Workspace\scripts\job_offer_plan.json"

3. See the report at:
   E:\AI-Agent-Workspace\output\job-offer-controller-test.md

4. See execution status and errors:
   py "E:\AI-Agent-Workspace\scripts\task_controller.py" status "E:\AI-Agent-Workspace\scripts\job_offer_plan.json"

CURRENT LIMITATIONS
-------------------
- Demo facts are already verified values supplied in the JSON plan; the controller does not yet connect to live Knowledge Search.
- An authorized person must review plans before execution; the JSON plan is treated as trusted input.
- This MVP offers deterministic arithmetic, file writing, file verification and step status, not general-purpose tool orchestration or LLM planning.
- No code execution, arbitrary network calls, writes outside output/ and temp/, or automatic retries.
- Running the same plan again starts a new execution and overwrites any explicitly approved targets. Do not run two instances of the same plan simultaneously.
