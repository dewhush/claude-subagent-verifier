#!/usr/bin/env bash
# End-to-end demo: snapshot, simulate a subagent edit, reconcile.
# Run from the repo root that contains .claude/skills/claude-subagent-verifier.
set -euo pipefail

CONTRACT=".claude/verifier/auth-refactor-001.contract.json"
SKILL_DIR=".claude/skills/claude-subagent-verifier"

mkdir -p "$(dirname "$CONTRACT")" src/auth tests/auth
cp "$SKILL_DIR/examples/auth-refactor.contract.json" "$CONTRACT"

# Pre-flight: must_modify file must already exist; must_create must not.
[ -f src/auth/__init__.py ] || echo "# placeholder" > src/auth/__init__.py
rm -f src/auth/oauth.py

echo "--- snapshot ---"
python "$SKILL_DIR/verifier.py" snapshot --contract "$CONTRACT" --force

echo "--- simulate subagent ---"
cat > src/auth/oauth.py <<'PY'
"""OAuth helper. Created by the subagent under test."""
def login(token: str) -> bool:
    return bool(token)
PY
echo "# touched by subagent" >> src/auth/__init__.py

echo "--- reconcile ---"
python "$SKILL_DIR/verifier.py" reconcile --contract "$CONTRACT" || {
  echo "REJECTED — see reasons above";
  exit 1;
}
echo "VERIFIED"
