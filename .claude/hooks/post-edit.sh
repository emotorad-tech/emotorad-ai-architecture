#!/usr/bin/env bash
# PostToolUse hook: compile-check the Python file Claude just edited, and run ruff if it is installed
# (the repo has no linter configured yet). Exit 2 sends findings back to Claude.
input="$(cat)"
file="$(printf '%s' "$input" | sed -n 's/.*"file_path":"\([^"]*\)".*/\1/p' | head -1)"
[ -n "$file" ] || exit 0
case "$file" in *.py) ;; *) exit 0 ;; esac
[ -f "$file" ] || exit 0
py="$(command -v python3 || command -v python || true)"; [ -n "$py" ] || exit 0
problems=""
out="$("$py" -m py_compile "$file" 2>&1)" || problems="$out"
if command -v ruff >/dev/null 2>&1; then
  out="$(ruff check --no-fix --select F,E9,B "$file" 2>&1)" || problems="${problems}
$out"
fi
if grep -nE '<<-?['"'"'"]?(EOF|PY|SH)' "$file" >/dev/null 2>&1 && grep -q '\\b' "$file"; then
  problems="${problems}
Regex written near a heredoc marker in $file: \\b can become a backspace byte. Define patterns in code, not through a shell heredoc."
fi
if [ -n "$problems" ]; then
  printf 'Problems in %s:\n%s\n' "$file" "$problems" >&2
  exit 2
fi
exit 0
