# Models

How well CoBirb works with the models it has been measured against, from its own offline
benchmark (`bench/`): small real tasks — fixing bugs, implementing from a spec, refactoring
across files, writing tests — each checked by tests the model never sees.

Measured at commit `603c292020`, 24 tasks × 2 runs each, on one local Ollama.

| Model | Tasks passed | Harder tasks | Median time per task |
|---|---|---|---|
| `ornith-1.5:9b` | 48/48 (100%) | 12/12 | 8s |
| `qwen3-coder:30b` | 48/48 (100%) | 12/12 | 15s |
| `ornith-1.5:35b` | 46/48 (96%) | 11/12 | 11s |
| `gpt-oss:20b` | 44/48 (92%) | 8/12 | 7s |
| `gemma4:latest` | 39/48 (81%) | 6/12 | 14s |

What the failures were, where there were any:

- `gemma4:latest`: 9× wrong result
- `gpt-oss:20b`: 2× wrong result, 1× no change, 1× turn limit
- `ornith-1.5:35b`: 1× wrong result, 1× error

These are small tasks on one machine; treat the numbers as a floor for what works, not a
ranking of the models. Any model that can call tools can be used — see
[config](config.md) for pointing CoBirb at yours.
