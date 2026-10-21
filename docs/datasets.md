# Benchmark data

`data/` is **not distributed with this repository**. It is listed in `.gitignore`
and removed from the Git index; a checkout leaves the directory absent.

Previously the directory held ~74 MB of third-party research artifacts. Those
files belong to their original projects, are governed by their own terms, and
several of them are gated behind an access agreement. Mirroring them inside an
MIT-licensed repository is not a redistribution right this project has.

## What the runtime expects

Nothing in `agents/` requires `data/` at runtime. Only the evaluation entry
points read it, and only when you point them at a file:

| Directory | Used by | Purpose |
| --- | --- | --- |
| `data/GAIA/` | `agents.eval_folding` | General assistant questions with reference answers |
| `data/HLE/` | `agents.eval_folding` | Humanity's Last Exam style questions |
| `data/ToolBench/` | offline analysis | Tool-use trajectories |
| `data/ToolHop/` | offline analysis | Multi-hop tool-use tasks |
| `data/API-Bank/` | offline analysis | API-calling dialogues |
| `data/ALFWorld/` | offline analysis | Text-based embodied tasks |
| `data/WebShop/` | offline analysis | Web shopping tasks |
| `data/RestBench/` | offline analysis | REST API tasks |
| `tests/fixtures/folding_eval_smoke.jsonl` | `agents.eval_folding` | Small local smoke set (this one is ours, committed) |

The one file this project authored lives at `tests/fixtures/folding_eval_smoke.jsonl`
and is committed; it does not depend on any external set.

## How to obtain the upstream sets

Fetch each set from its original publisher and keep the existing directory
layout (`data/<name>/`):

- **GAIA** — published by the GAIA benchmark authors; the official Hugging Face
  dataset is gated and requires accepting its terms.
- **HLE (Humanity's Last Exam)** — published by the Center for AI Safety and
  Scale AI; the official Hugging Face dataset is gated.
- **ToolBench** — OpenBMB; code is Apache-2.0, and the data carries additional
  terms around model-generated content.
- **ToolHop**, **API-Bank**, **RestBench**, **ALFWorld**, **WebShop** — see each
  project's paper and official repository for the current license and download
  instructions.

Do not commit the downloaded files. Do not assume the license of one set applies
to another.

## Evaluations that do not need external data

- `tests/` — 213 offline cases, no API calls, no datasets.
- `tests/fixtures/folding_eval_smoke.jsonl` — a handful of hand-written cases for a quick
  end-to-end check of `eval_folding`.

Both are enough to verify runtime behaviour. Benchmark accuracy numbers require
the real sets and must be reported with the exact snapshot you used.
