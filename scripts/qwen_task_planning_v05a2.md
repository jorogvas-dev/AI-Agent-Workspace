# Qwen v0.5a.2 intent contract

Return only the strict intent JSON object. Do not return a task plan. Python
will compile the intent into a plan using fixed extraction capabilities.

The only supported document is `sample.pdf`. The only supported fields are
`hourly_wage`, `weekly_hours`, and `housing_per_day`. The only calculations
are `weekly_gross`, `weekly_housing`, and `difference`. The only numeric
assumption is exactly `days_per_week=7 days/week`.

Do not output document values, quotes, evidence, regexes, formulas, templates,
paths, URLs, shell commands, Python, or extra fields. A valid intent does not
mean a task was retrieved or executed. Python will perform no retrieval or
persistent write in v0.5a.2.
