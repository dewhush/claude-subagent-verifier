# Examples

Two ready-to-run scenarios.

## 1. Happy path: `auth-refactor.contract.json`

Demonstrates the verifier returning `VERIFIED` after a subagent creates a new
file, modifies an existing one, and leaves untouched paths alone. Run end to
end with [`run_demo.sh`](run_demo.sh).

```bash
bash examples/run_demo.sh
```

Expected last line: `VERIFIED`.

## 2. Failure path: `rejected-noop.contract.json`

Demonstrates the no-op detector. Snapshot the contract, then run reconcile
without making any changes. The verifier returns `REJECTED` with reasons:

- `must_create not present: docs/architecture.md`
- `check failed: bash -lc test -s docs/architecture.md (exit 1) | ...`

```bash
python .claude/skills/claude-subagent-verifier/verifier.py snapshot \
  --contract examples/rejected-noop.contract.json --force
python .claude/skills/claude-subagent-verifier/verifier.py reconcile \
  --contract examples/rejected-noop.contract.json
```

Exit code will be `1`.

## Wiring into a real Claude Code workflow

In your project's `CLAUDE.md`:

```
Never act on a subagent result unless its final message contains a VERIFIED
block emitted by claude-subagent-verifier; on REJECTED, re-dispatch with the
failure reasons appended to the prompt.
```

In your dispatch step, before calling the Task tool or `claude --bg --exec`:

```bash
python .claude/skills/claude-subagent-verifier/verifier.py snapshot \
  --contract .claude/verifier/<task-id>.contract.json
```

In your post-dispatch step:

```bash
python .claude/skills/claude-subagent-verifier/verifier.py reconcile \
  --contract .claude/verifier/<task-id>.contract.json
```

Pipe the reconcile JSON straight back to the parent agent. That is the
`VERIFIED` / `REJECTED` block the `CLAUDE.md` rule keys off of.
