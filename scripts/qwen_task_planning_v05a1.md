# Qwen v0.5a.1 generation contract

Python, not Qwen, controls plan generation and validation. Return one strict
JSON object only. Do not call validation tools directly. Python may request at
most one correction generation after the first result fails validation.

Use only the v0.5a operations: `search_knowledge`, `extract_number`,
`calculate`, `write_report`, and `verify_file`. Use typed variables, explicit
dependencies, and paths under `output/` or `temp/`. Never output JavaScript,
`.repeat()`, Python, shell, URLs, markdown fences, comments, or invented
operations. A structurally valid plan can still be incomplete; include every
operation needed by the user’s request. Planning and preview do not retrieve,
write, approve, commit, or execute anything.
