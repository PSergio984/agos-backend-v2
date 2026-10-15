#!/usr/bin/env bash
# PostToolUse hook (exec/bash matcher): rebuild the local graphify graph
# after a command that may have advanced HEAD. Portable — no-ops when
# graphify or git aren't on PATH. Run from the repo root (hook cwd).
set -u

INPUT=$(cat 2>/dev/null || true)
case "$INPUT" in
  *"git commit"*|*"git merge"*|*"git pull"*|*"git rebase"*|*"git cherry-pick"*) ;;
  *) exit 0 ;;
esac

command -v graphify >/dev/null 2>&1 || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

(graphify update . >/dev/null 2>&1 &)
exit 0
