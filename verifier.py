#!/usr/bin/env python3
"""claude-subagent-verifier: snapshot + reconcile a Claude Code subagent run.

This is a deterministic, side-effect-free verifier. It does NOT execute the
subagent; it only records pre-flight state and diffs it against post-flight
state, then runs the declared test/lint commands. Output is a single JSON
line on stdout so the parent agent can paste it back as a structured block.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA_REQUIRED = ["task_id", "subagent_type"]
SCHEMA_OPTIONAL = [
    "must_create",
    "must_modify",
    "must_delete",
    "must_not_touch",
    "must_pass",
    "must_lint_clean",
    "worktree",
]
RUNTIME_DIR = Path(".claude/verifier")


def die(code: int, payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


def load_contract(path: Path) -> dict[str, Any]:
    if not path.is_file():
        die(2, {"status": "ERROR", "reason": f"contract not found: {path}"})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(2, {"status": "ERROR", "reason": f"invalid contract JSON: {e}"})
    for k in SCHEMA_REQUIRED:
        if k not in data:
            die(2, {"status": "ERROR", "reason": f"contract missing required field: {k}"})
    for k in data.keys():
        if k not in SCHEMA_REQUIRED + SCHEMA_OPTIONAL:
            die(2, {"status": "ERROR", "reason": f"contract has unknown field: {k}"})
    return data


def repo_root(worktree: str | None) -> Path:
    if worktree:
        p = Path(worktree).resolve()
        if not p.is_dir():
            die(2, {"status": "ERROR", "reason": f"worktree not a dir: {p}"})
        return p
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        die(2, {"status": "ERROR", "reason": "not inside a git repo"})
    return Path(out.stdout.strip())


def head_sha(root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def hash_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def gather_paths(contract: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("must_create", "must_modify", "must_delete"):
        paths.extend(contract.get(key, []) or [])
    return sorted(set(paths))


def expand_globs(root: Path, globs: list[str]) -> list[Path]:
    out: list[Path] = []
    for g in globs or []:
        for p in root.rglob("*"):
            rel = p.relative_to(root).as_posix()
            if fnmatch.fnmatch(rel, g):
                if p.is_file():
                    out.append(p)
    return sorted(set(out))


def hash_tree(root: Path, paths: list[str]) -> dict[str, str | None]:
    return {p: hash_file(root / p) for p in paths}


def hash_globs(root: Path, globs: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for p in expand_globs(root, globs):
        rel = p.relative_to(root).as_posix()
        out[rel] = hash_file(p)
    return out


def runtime_paths(root: Path, task_id: str) -> tuple[Path, Path]:
    d = root / RUNTIME_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{task_id}.snapshot.json", d / f"{task_id}.result.json"


def cmd_snapshot(args: argparse.Namespace) -> None:
    contract = load_contract(Path(args.contract))
    root = repo_root(args.worktree or contract.get("worktree"))
    snap_path, _ = runtime_paths(root, contract["task_id"])
    if snap_path.exists() and not args.force:
        die(2, {"status": "ERROR", "reason": f"snapshot already exists: {snap_path}; pass --force to overwrite"})
    declared = gather_paths(contract)
    no_touch_globs = contract.get("must_not_touch", []) or []
    snapshot = {
        "task_id": contract["task_id"],
        "head_sha": head_sha(root),
        "root": str(root),
        "declared_hashes": hash_tree(root, declared),
        "no_touch_hashes": hash_globs(root, no_touch_globs),
    }
    snap_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(json.dumps({"status": "SNAPSHOT", "task_id": contract["task_id"], "snapshot": str(snap_path), "head_sha": snapshot["head_sha"]}))


def run_check(spec: dict[str, Any], cwd: Path) -> tuple[bool, str]:
    cmd = spec.get("cmd")
    if not isinstance(cmd, list) or not cmd:
        return False, "check missing cmd[]"
    timeout = float(spec.get("timeout_s", 60))
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"check timed out after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return False, f"check executable missing: {e}"
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        return False, f"check failed: {' '.join(cmd)} (exit {proc.returncode}) | {' / '.join(tail)}"
    return True, f"check passed: {' '.join(cmd)}"


def cmd_reconcile(args: argparse.Namespace) -> None:
    contract = load_contract(Path(args.contract))
    root = repo_root(args.worktree or contract.get("worktree"))
    snap_path, result_path = runtime_paths(root, contract["task_id"])
    if not snap_path.exists():
        die(2, {"status": "ERROR", "reason": f"missing snapshot: {snap_path}; run snapshot first"})
    snapshot = json.loads(snap_path.read_text(encoding="utf-8"))

    reasons: list[str] = []
    files_created = 0
    files_modified = 0
    files_deleted = 0
    checks_passed = 0
    checks_total = 0

    pre_declared: dict[str, str | None] = snapshot["declared_hashes"]
    pre_no_touch: dict[str, str | None] = snapshot["no_touch_hashes"]

    for p in contract.get("must_create", []) or []:
        post = hash_file(root / p)
        pre = pre_declared.get(p)
        if pre is not None:
            reasons.append(f"must_create existed before run: {p}")
        elif post is None:
            reasons.append(f"must_create not present: {p}")
        else:
            files_created += 1

    for p in contract.get("must_modify", []) or []:
        post = hash_file(root / p)
        pre = pre_declared.get(p)
        if pre is None:
            reasons.append(f"must_modify did not exist before run: {p}")
        elif post is None:
            reasons.append(f"must_modify was deleted: {p}")
        elif post == pre:
            reasons.append(f"must_modify unchanged: {p}")
        else:
            files_modified += 1

    for p in contract.get("must_delete", []) or []:
        post = hash_file(root / p)
        pre = pre_declared.get(p)
        if pre is None:
            reasons.append(f"must_delete did not exist before run: {p}")
        elif post is not None:
            reasons.append(f"must_delete still present: {p}")
        else:
            files_deleted += 1

    # must_not_touch: any path whose hash changed (or appeared/disappeared) is a violation.
    post_no_touch = hash_globs(root, contract.get("must_not_touch", []) or [])
    all_keys = set(pre_no_touch) | set(post_no_touch)
    for k in sorted(all_keys):
        if pre_no_touch.get(k) != post_no_touch.get(k):
            reasons.append(f"must_not_touch violated: {k}")

    # No-op detection: nothing claimed, nothing changed.
    if (
        not contract.get("must_create")
        and not contract.get("must_modify")
        and not contract.get("must_delete")
        and files_created == files_modified == files_deleted == 0
    ):
        reasons.append("no_op_subagent: contract declared no file outcomes and nothing changed")

    for spec in contract.get("must_pass", []) or []:
        checks_total += 1
        ok, msg = run_check(spec, root)
        if ok:
            checks_passed += 1
        else:
            reasons.append(msg)

    for spec in contract.get("must_lint_clean", []) or []:
        checks_total += 1
        ok, msg = run_check(spec, root)
        if ok:
            checks_passed += 1
        else:
            reasons.append(msg)

    status = "VERIFIED" if not reasons else "REJECTED"
    result: dict[str, Any] = {
        "task_id": contract["task_id"],
        "status": status,
        "files_created": files_created,
        "files_modified": files_modified,
        "files_deleted": files_deleted,
        "checks_passed": checks_passed,
        "checks_total": checks_total,
        "head_sha_pre": snapshot.get("head_sha"),
        "head_sha_post": head_sha(root),
    }
    if reasons:
        result["reasons"] = reasons
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if status == "VERIFIED" else 1)


def main() -> None:
    p = argparse.ArgumentParser(prog="claude-subagent-verifier")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="Capture pre-flight state from a contract")
    s.add_argument("--contract", required=True)
    s.add_argument("--worktree")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_snapshot)
    r = sub.add_parser("reconcile", help="Diff post-flight state and run checks")
    r.add_argument("--contract", required=True)
    r.add_argument("--worktree")
    r.set_defaults(func=cmd_reconcile)
    args = p.parse_args()
    # Run from the repo's CWD by default; commands are resolved against repo root.
    os.chdir(repo_root(getattr(args, "worktree", None) or None))
    args.func(args)


if __name__ == "__main__":
    main()
