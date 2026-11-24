#!/usr/bin/env bash
# PreToolUse (Bash): run the unittest suite before `git commit`; block the commit if it fails.
# The script checks the command itself (the settings `if` is only an optimisation): any other Bash call exits 0
# at once, so a red suite never blocks ordinary work. API keys are stripped so no test can reach a real LLM.
input=$(cat)
cmd=$(printf '%s' "$input" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("tool_input", {}).get("command", ""))
except Exception: print("")' 2>/dev/null)
printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])git([[:space:]]+-[^[:space:]]+([[:space:]]+[^-[:space:]][^[:space:]]*)?)*[[:space:]]+commit([[:space:]]|$)' || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
py=".venv/bin/python"; [ -x "$py" ] || py=python3
out=$(env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u RAIYA_OPENAI_API_KEY "$py" -m unittest discover -q 2>&1) && exit 0
echo "Test suite failed; commit blocked."$'\n'"$(echo "$out" | tail -25)" >&2
exit 2
