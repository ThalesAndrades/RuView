#!/usr/bin/env node
// RuView ReasoningBank — seed + query an AgentDB-backed agent memory with the
// distilled patterns/episodes from this project's engineering history.
//
// This lives in the DEV/COORDINATION layer (agent memory), NOT the RuView
// product runtime — the edge product uses ruvector (Rust, in-process) for its
// own vector search. AgentDB (Node) is the right home for cross-session
// ReasoningBank memory with hybrid (vector + metadata) retrieval, skill search,
// causal-edge learning, and QUIC fleet sync.
//
// Usage:
//   node reasoningbank.mjs seed
//   node reasoningbank.mjs query "false alarms after a restart"
//   node reasoningbank.mjs query "how do I know a feature is actually wired up" --skills
//   node reasoningbank.mjs query "bug fix" --filters '{"success":true,"reward":{"$gte":0.9}}'
//
// Env: AGENTDB_PATH (default ./.agentdb/reasoningbank.db). Run
// `npx agentdb@latest install-embeddings` once for real MiniLM embeddings
// (otherwise AgentDB uses mock embeddings and similarity scores are not
// meaningful — the store/retrieve plumbing still works).

import { execFileSync } from "node:child_process";
import { readFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const DB = process.env.AGENTDB_PATH || join(HERE, ".agentdb", "reasoningbank.db");
mkdirSync(dirname(DB), { recursive: true }); // AgentDB won't create the dir itself
const env = { ...process.env, AGENTDB_PATH: DB };

function agentdb(args, { quiet = false } = {}) {
  try {
    const out = execFileSync("npx", ["--yes", "agentdb@latest", ...args], {
      env,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    return out;
  } catch (e) {
    // AgentDB v2 CLI currently throws `this.db.save is not a function` on the
    // persistent-store path even though the episode is written; surface it but
    // don't abort the whole seed.
    if (!quiet) process.stderr.write(`  (agentdb warning) ${String(e.stderr || e.message).split("\n")[0]}\n`);
    return e.stdout || "";
  }
}

function readJsonl(name) {
  return readFileSync(join(HERE, name), "utf8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l));
}

function seed() {
  console.log(`\n  Seeding RuView ReasoningBank → ${DB}\n`);
  agentdb(["init", DB, "--dimension", "384"], { quiet: true });

  const episodes = readJsonl("episodes.jsonl");
  for (const ep of episodes) {
    agentdb([
      "reflexion", "store",
      ep.session_id, ep.task,
      String(ep.reward), String(ep.success),
      ep.critique ?? "", ep.input ?? "", ep.output ?? "",
    ]);
    console.log(`  · episode  ${ep.session_id.padEnd(20)} (reward ${ep.reward})`);
  }

  const skills = readJsonl("skills.jsonl");
  for (const sk of skills) {
    agentdb(["skill", "create", sk.name, sk.description]);
    console.log(`  · skill    ${sk.name}`);
  }

  console.log(`\n  Seeded ${episodes.length} episodes + ${skills.length} skills.`);
  console.log(`  Query:  node reasoningbank.mjs query "<your question>"\n`);
}

function query(text, opts) {
  if (opts.skills) {
    console.log(`\n  skill search: "${text}"\n`);
    process.stdout.write(agentdb(["skill", "search", text, String(opts.k)]));
    return;
  }
  console.log(`\n  reflexion retrieve: "${text}"${opts.filters ? `  filters=${opts.filters}` : ""}\n`);
  const args = ["reflexion", "retrieve", text, "--k", String(opts.k), "--synthesize-context"];
  if (opts.filters) args.push("--filters", opts.filters);
  process.stdout.write(agentdb(args));
}

// ── arg parsing ──────────────────────────────────────────────────────────
const [cmd, ...rest] = process.argv.slice(2);
const opts = { k: 5, skills: false, filters: null };
const positional = [];
for (let i = 0; i < rest.length; i++) {
  if (rest[i] === "--skills") opts.skills = true;
  else if (rest[i] === "--k") opts.k = Number(rest[++i]);
  else if (rest[i] === "--filters") opts.filters = rest[++i];
  else positional.push(rest[i]);
}

if (cmd === "seed") seed();
else if (cmd === "query" && positional.length) query(positional.join(" "), opts);
else {
  console.log(`RuView ReasoningBank (AgentDB-backed agent memory)

  node reasoningbank.mjs seed
  node reasoningbank.mjs query "<question>" [--k N] [--filters '<mongo-json>']
  node reasoningbank.mjs query "<question>" --skills

  One-time, for real embeddings:  npx agentdb@latest install-embeddings`);
  process.exit(positional.length || cmd ? 1 : 0);
}
