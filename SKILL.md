# claude-subagent-verifier

Reconcile what a Claude Code subagent **claims** it did against what is **actually true on disk and in git**, before the parent session trusts the result.

## Why this exists

Claude Code 2.1.154 introduced dynamic workflows that orchestrate "tens to hundreds of agents in the background". The 2.1.157 changelog (May 29, 2026) shipped fixes for leaked background shells, orphaned worktrees under `.claude/worktrees/`, and `--resume` losing track of running subagents. These are symptoms of a deeper weakness:

> The parent session has no enforced way to verify that a finished subagent actually produced the artifacts it reported. Failures get hand-waved into the parent's context as "done", and downstream agents act on lies.

`claude-subagent-verifier` is a drop-in skill that:

- Captures a **pre-flight snapshot** of the worktree (sha + file digests for the paths the subagent is allowed to touch).
- Records the subagent's **declared contract**: which paths it must create/modify, which tests must pass, which lints must be clean.
- After the subagent reports completion, runs a **post-flight reconciler** that diffs claimed vs actual, runs the declared checks, and either:
  - returns a structured `VERIFIED` block to the parent, or
  - emits a `REJECTED` block with the precise mismatch (missing file, untouched path, failing test, lint regression).

The parent agent is **forbidden by the SKILL.md contract** from accepting subagent output that lacks a `VERIFIED` block.

## When to load this skill

Trigger on any of:

- The user says "spawn subagents", "run workflow", "dispatch background agents", "parallelize this".
- A `/workflows` or `claude --bg --exec` invocation is being prepared.
- You are about to call the Task tool with `subagent_type` for non-trivial code edits.
- The current task touches more than one file and you intend to delegate.

Do **not** load for read-only research subagents.

## Install

```bash
# In the repo where Claude Code runs (auto-loaded from .claude/skills as of 2.1.157)
mkdir -p .claude/skills
git clone https://github.com/dewhush/claude-subagent-verifier .claude/skills/claude-subagent-verifier
```

Or as a per-user skill:

```bash
mkdir -p ~/.claude/skills
git clone https://github.com/dewhush/claude-subagent-verifier ~/.claude/skills/claude-subagent-verifier
```

No build step. The skill ships a single Python script (`verifier.py`) and a JSON schema (`contract.schema.json`).

## Usage

### 1. Before dispatching a subagent

Write a contract file at `.claude/verifier/<task-id>.contract.json`:

```json
{
  "task_id": "auth-refactor-001",
  "subagent_type": "general-purpose",
  "must_create": ["src/auth/oauth.py"],
  "must_modify": ["src/auth/__init__.py"],
  "must_not_touch": ["src/billing/**", "migrations/**"],
  "must_pass": [
    {"cmd": ["pytest", "tests/auth", "-q"], "timeout_s": 120}
  ],
  "must_lint_clean": [
    {"cmd": ["ruff", "check", "src/auth"], "timeout_s": 30}
  ]
}
```

Then run:

```bash
python .claude/skills/claude-subagent-verifier/verifier.py snapshot \
  --contract .claude/verifier/auth-refactor-001.contract.json
```

This stores `.claude/verifier/auth-refactor-001.snapshot.json` with the pre-flight git sha and content hashes of every path the subagent is allowed to touch.

### 2. Dispatch the subagent normally

Use the Task tool, `claude --bg --exec`, or `/workflows` as you would today.

### 3. After the subagent reports back

```bash
python .claude/skills/claude-subagent-verifier/verifier.py reconcile \
  --contract .claude/verifier/auth-refactor-001.contract.json
```

Output is a single line of JSON. Examples:

```json
{"task_id":"auth-refactor-001","status":"VERIFIED","files_created":1,"files_modified":1,"checks_passed":2}
```

```json
{"task_id":"auth-refactor-001","status":"REJECTED","reasons":["must_create not present: src/auth/oauth.py","check failed: pytest tests/auth (exit 1)"]}
```

### 4. Parent-side rule

Add this single line to your `CLAUDE.md` so the parent agent self-enforces the contract:

```
Never act on a subagent result unless its final message contains a VERIFIED block emitted by claude-subagent-verifier; on REJECTED, re-dispatch with the failure reasons appended to the prompt.
```

## Validation checklist

Before calling a run "successful":

- [ ] `snapshot` produced a `*.snapshot.json` whose `head_sha` matches `git rev-parse HEAD` at dispatch time.
- [ ] `reconcile` exit code is `0` for VERIFIED, `1` for REJECTED, `2` for contract/schema errors.
- [ ] Every path in `must_create` exists and was absent in the snapshot.
- [ ] Every path in `must_modify` has a different content hash than the snapshot.
- [ ] No path matching `must_not_touch` globs has a different content hash than the snapshot.
- [ ] Every `must_pass` and `must_lint_clean` command exited 0.

## Troubleshooting

- **"snapshot already exists"** — a previous run's snapshot was not consumed. Delete `.claude/verifier/<task-id>.snapshot.json` or pass `--force`.
- **"path matched must_not_touch but is unchanged"** — false positive; the verifier only flags paths whose hash actually changed. If you still see this, your `.gitignore` is hiding a tracked file.
- **Subagent says "done" but no files changed** — `reconcile` will return REJECTED with `no_op_subagent: true`. This catches Claude Code's known habit of declaring success after only reading files.
- **Tests pass but lint fails** — REJECTED. The skill does not let lint regressions slip through; remove the lint check from the contract if you genuinely want to defer it.
- **Worktree subagents (background)** — pass `--worktree <path>` to both `snapshot` and `reconcile`; the verifier will resolve hashes inside that worktree instead of the canonical repo.

## What this skill deliberately does NOT do

- It does not run the subagent for you.
- It does not parse the subagent's natural-language reply. Truth comes from disk and git, not from text.
- It does not auto-fix REJECTED runs. That is the parent agent's job.
