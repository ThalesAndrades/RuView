# RuView ReasoningBank (AgentDB-backed agent memory)

An [AgentDB](https://github.com/ruvnet/agentic-flow/tree/main/packages/agentdb)
v2 ReasoningBank seeded with the **distilled patterns and episodes from this
project's engineering history** — so a future agent (or human) can *retrieve*
the lessons instead of relearning them.

## Why AgentDB here (and ruvector in the product)

This is the **dev / coordination layer** — cross-session *agent memory*. It is
deliberately separate from the RuView **product runtime**, which already does
its own vector search with **`ruvector`** (Rust, in-process, HNSW — see
`homecore-recorder`, `cog-ha-matter`). Putting a Node vector DB on the edge
device would be redundant and the wrong stack. AgentDB is the right home for a
ReasoningBank: hybrid (vector + metadata) retrieval, skill search, causal-edge
learning, and QUIC fleet sync, all in the Node tooling layer.

## Contents

- **`episodes.jsonl`** — one *reflexion episode* per significant decision/PR in
  this engagement (task, reward, success, and a **critique = the lesson**:
  problem → fix). Maps to `agentdb reflexion store`.
- **`skills.jsonl`** — the cross-cutting, reusable **skills** distilled from
  those episodes (e.g. `detect-dead-wiring`, `safety-by-construction-guards`,
  `cut-flawed-design-under-review`, `honest-accuracy-reporting`). Maps to
  `agentdb skill create`.
- **`reasoningbank.mjs`** — `seed` + `query` driver over the AgentDB CLI.

The `.jsonl` files are the durable asset; the `.agentdb/` database is generated
and git-ignored.

## Usage

```bash
cd tools/reasoningbank

# one-time, for real MiniLM embeddings (else AgentDB uses mock embeddings and
# similarity scores aren't meaningful — the store/retrieve plumbing still works)
npx agentdb@latest install-embeddings

node reasoningbank.mjs seed                                   # populate the bank
node reasoningbank.mjs query "false alarms after a restart"   # semantic recall + synthesized context
node reasoningbank.mjs query "is my feature actually wired up" --skills
node reasoningbank.mjs query "bug fix" --filters '{"success":true,"reward":{"$gte":0.9}}'
```

`AGENTDB_PATH` overrides the database location (default `./.agentdb/reasoningbank.db`).

## `agentdb-advanced` features this exercises

| Feature | Here |
|---|---|
| **Reflexion store/retrieve** with self-critique | each episode carries the lesson; `query` returns ranked episodes + a synthesized-context summary |
| **Hybrid search** (vector + MongoDB-style metadata filters) | `--filters '{"success":true,"reward":{"$gte":0.9}}'` |
| **Skill memory** (`skill create` / `skill search`) | the distilled reusable patterns, retrievable by similarity |
| **Causal learning** (`agentdb learner run`) | can mine causal edges from the episodes (e.g. *audit-before-claiming → high reward*) |
| **QUIC sync** (`agentdb sync push/pull --server host:4433`) | share one bank across a team/fleet at <1 ms |

## Honest caveats

- Without `install-embeddings`, AgentDB falls back to **mock embeddings** —
  retrieval *works* but the similarity ranking isn't semantically meaningful.
- The AgentDB v2 CLI currently prints `this.db.save is not a function` on the
  persistent-store path; it is benign here (the SQLite episode/skill rows are
  still written and are retrievable — verified), but it means the GNN index
  isn't persisted. Re-seeding is cheap and idempotent enough for this use.
