#!/usr/bin/env bash
# PostToolUse (Write|Edit): fast syntax check on edited .py files.
# Exit 2 feeds the error back to Claude; anything else is silent.
py="${CLAUDE_PROJECT_DIR:-.}/.venv/bin/python"; [ -x "$py" ] || py=python3
f=$("$py" -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null)
case "$f" in *.py) ;; *) exit 0 ;; esac
[ -f "$f" ] || exit 0
out=$("$py" -m py_compile "$f" 2>&1) && exit 0
echo "py_compile failed for $f:"$'\n'"$out" >&2
exit 2
