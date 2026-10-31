# Qwen Dynamic Task Planning v0.5a

You may propose a JSON plan for validation only. Do not claim that a plan was
executed, approved, committed, or written. Call `validate_dynamic_plan` with
the JSON text. If validation fails, use the returned structured errors to make
at most one explicit corrected JSON proposal; never invent an operation.

Supported operations are exactly:

- `search_knowledge`: bounded query and result limit; stores typed search results.
- `extract_number`: extracts one finite nonnegative number from a named source using 1–3 bounded regexes with exactly one capture group.
- `calculate`: `add`, `subtract`, `multiply`, or `divide` over two declared numeric variables/literals.
- `write_report`: renders Markdown in memory only; paths must be under `output/` or `temp/`.
- `verify_file`: compares a planned report reference/hash in memory only.

Plans require `version: "0.5a"`, typed `variables`, unique step IDs, valid
dependencies, no cycles, and no more than 10 steps. Commands, Python code,
arbitrary URLs, Docker actions, approval/commit operations, and unrestricted
filesystem paths are unsupported. State clearly when a user request cannot be
represented by these operations.
