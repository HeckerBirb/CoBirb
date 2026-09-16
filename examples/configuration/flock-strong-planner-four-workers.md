# Flock: strong planner, four workers

A [flock](../../docs/manual/flock.md) run where Brainy Birb gets the most capable model you
have, and the four Worker Birbs share a smaller model that is still strong enough to be trusted
alone with a file — not a distilled toy.

```json
{
  "models": {
    "default":      { "name": "qwen2.5-coder:32b" },
    "orchestrator": { "name": "qwen2.5-coder:32b" },
    "worker":       { "name": "qwen2.5-coder:14b" }
  }
}
```

`cobirb models` confirms how each role resolves before you commit an endpoint to it.

## The charter side

Model choice lives in config; **how many** workers run is a per-run decision Brainy Birb makes
when it writes the charter, up to `concurrency`'s default cap of 16. Nudge it towards four
workers in the request itself:

```bash
cobirb flock -p "split the reporting rewrite across four workers: \
csv export, PDF export, the shared formatter, and the test fixtures"
```

Brainy Birb still decides the actual seams — a charter that comes back with four `[[workers]]`
blocks might look like:

```toml
objective = "rewrite the reporting module's three exporters and their shared formatter"
concurrency = 4

[[workers]]
id = "csv-exporter"
writes = ["src/reporting/export/csv.py"]
reads  = ["src/reporting/"]
accept = "pytest tests/reporting/test_csv_export.py"
brief  = "Implement write_csv() against the stub's docstring."

[[workers]]
id = "pdf-exporter"
writes = ["src/reporting/export/pdf.py"]
reads  = ["src/reporting/"]
accept = "pytest tests/reporting/test_pdf_export.py"
brief  = "Implement write_pdf() against the stub's docstring."

[[workers]]
id = "formatter"
writes = ["src/reporting/format.py"]
reads  = ["src/reporting/"]
accept = "pytest tests/reporting/test_format.py"
brief  = "Implement the shared number and date formatting helpers."

[[workers]]
id = "fixtures"
writes = ["tests/reporting/fixtures.py"]
reads  = ["src/reporting/"]
accept = "pytest tests/reporting/"
brief  = "Build the shared sample-report fixtures the other three test files import."
```

You still approve this charter — worker count, model choice and all — before anything runs.

## Why a strong-but-smaller worker model

Workers get their brief and nothing else: no plan, no project instructions, no memory. A model
too weak to reason about a docstring-only stub in isolation will produce code that looks right
and fails review; the model above the "toy" tier is the one worth the concurrency.
