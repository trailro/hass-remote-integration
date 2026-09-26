#!/bin/sh
# What the weekly app canary does with its results (.github/workflows/app-canary.yml).
#   - a job failed: an issue "[app-canary] ..." (one per set of versions, commented on rather than repeated);
#   - the release scan found notes: the issue "[app-canary] release notes to review", commented on only when the
#     notes cover something it has not said yet;
#   - something moved since .github/app_versions.json and the stable path passed (the schema on the stable
#     Supervisor, the linter, the Home Assistant OS boot): a pull request on app-canary/<versions> that moves the
#     record, and with it the Supervisor tag ci.yml checks against.  Never twice for the same versions: an open or a
#     closed-unmerged pull request of that branch stops it, and an older app-canary pull request still open is closed
#     in favour of the new one.  docs/app.md ("tested on") is maintained by hand and never touched here.
# Reads VERSIONS_RESULT, SCHEMA_STABLE, SCHEMA_BETA, SCHEMA_MAIN, LINT_RESULT, HAOS_RESULT (job or leg conclusions),
# NOTES_FOUND and NOTES_FILE, CHANGED, MOVED, PROPOSED (the record to propose, JSON), BRANCH, SUPERVISOR,
# SUPERVISOR_BETA, HAOS, CORE, RUN, GH_SERVER, GH_REPO and GH_TOKEN.  DRY_RUN=1 prints what would be written to
# GitHub (issues, comments, pull requests, pushes, workflow runs) instead of doing it; everything read still runs.
set -eu

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

write() {  # a command that changes something on GitHub
  if [ "${DRY_RUN:-0}" = 1 ]; then
    echo "DRY_RUN: $*"
  else
    "$@"
  fi
}

issue_number() {  # the open issue with exactly this title, if any
  gh issue list --state open --search "app-canary in:title" --json number,title \
    --jq "map(select(.title == \"$1\")) | .[0].number // empty"
}

versions="Supervisor ${SUPERVISOR:-?} (beta ${SUPERVISOR_BETA:-?}), Home Assistant OS ${HAOS:-?}, Core ${CORE:-?}"

# 1. failures
failed=""
add() { failed="$failed
| $1 | \`$2\` |"; }
[ "${VERSIONS_RESULT:-}" = success ] || add "asking which versions to test" "${VERSIONS_RESULT:-missing}"
for leg in "stable ${SUPERVISOR:-}:${SCHEMA_STABLE:-}" "beta ${SUPERVISOR_BETA:-}:${SCHEMA_BETA:-}" "main:${SCHEMA_MAIN:-}"; do
  name=${leg%%:*}
  result=${leg#*:}
  case "$result" in success|skipped|"") ;; *) add "Supervisor schema, $name" "$result" ;; esac
done
case "${LINT_RESULT:-}" in success|skipped|"") ;; *) add "app linter, newest release" "$LINT_RESULT" ;; esac
case "${HAOS_RESULT:-}" in success|skipped|"") ;; *) add "Home Assistant OS boot, install, backup and restore" "$HAOS_RESULT" ;; esac
if [ -n "$failed" ]; then
  title="[app-canary] the app fails on $versions"
  cat > "$tmp/fail.md" <<EOF
The weekly app canary failed.

| job | result |
|---|---|$failed

[The run]($RUN) says which step. The schema jobs run the Supervisor's own validation of \`app/config.yaml\`
(\`.github/app_supervisor_check.py\`); the Home Assistant OS job boots the newest stable OS in a VM, installs the
app from this repository and takes a backup of it and restores it.
EOF
  number=$(issue_number "$title")
  if [ -n "$number" ]; then
    write gh issue comment "$number" --body-file "$tmp/fail.md"
  else
    write gh issue create --title "$title" --body-file "$tmp/fail.md"
  fi
fi
if [ "${VERSIONS_RESULT:-}" != success ]; then
  exit 0
fi

# 2. release notes
if [ "${NOTES_FOUND:-false}" = true ] && [ -s "${NOTES_FILE:-}" ]; then
  title="[app-canary] release notes to review"
  marker=$(head -n1 "$NOTES_FILE")
  number=$(issue_number "$title")
  if [ -z "$number" ]; then
    write gh issue create --title "$title" --body-file "$NOTES_FILE"
  elif gh issue view "$number" --json body,comments --jq '[.body] + [.comments[].body] | join("\n")' | grep -qxF "$marker"; then
    echo "issue #$number already covers these notes"
  else
    write gh issue comment "$number" --body-file "$NOTES_FILE"
  fi
fi

# 3. a pull request that moves the record
if [ "${CHANGED:-false}" != true ]; then
  echo "nothing moved since .github/app_versions.json"
  exit 0
fi
if [ "${SCHEMA_STABLE:-}" != success ] || [ "${LINT_RESULT:-}" != success ] || [ "${HAOS_RESULT:-}" != success ]; then
  echo "the stable path did not pass (schema ${SCHEMA_STABLE:-missing}, linter ${LINT_RESULT:-missing}, Home Assistant OS ${HAOS_RESULT:-missing}): nothing to propose"
  exit 0
fi
states=$(gh pr list --head "$BRANCH" --state all --json state --jq 'map(.state) | join(" ")')
case " $states " in
  *" OPEN "*) echo "a pull request of $BRANCH is already open"; exit 0 ;;
  *" CLOSED "*) echo "a pull request of $BRANCH was closed without merging: not proposing it again"; exit 0 ;;
  *" MERGED "*) echo "a pull request of $BRANCH was merged already"; exit 0 ;;
esac

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git checkout -q -b "$BRANCH"
printf '%s' "$PROPOSED" | python3 -c 'import json, sys; json.dump(json.load(sys.stdin), sys.stdout, indent=2); print()' > .github/app_versions.json
if git diff --quiet; then
  echo ".github/app_versions.json already records these versions"
  exit 0
fi
cat > "$tmp/commit.txt" <<EOF
record the app's platform versions the canary passed on

$MOVED

The weekly app canary checked app/config.yaml with the Supervisor's own validation,
ran the newest app linter, and installed, backed up and restored the app on
Home Assistant OS $HAOS.  CI checks the app against the Supervisor recorded here.
EOF
cat > "$tmp/pr.md" <<EOF
The app canary passed on $versions.

What moved since \`.github/app_versions.json\`: $MOVED.

This moves the record, and with it the Supervisor tag the CI \`app\` job validates \`app/config.yaml\` against. The
"tested on" line of \`docs/app.md\` is maintained by hand: update it in this pull request if it should change.

[The canary run]($RUN).
EOF
git commit -qaF "$tmp/commit.txt"
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  echo "the branch $BRANCH is already on the remote; not pushing again"
else
  write git push -q origin "$BRANCH"
fi
if write gh pr create --head "$BRANCH" --base main --title "app canary: $MOVED" --body-file "$tmp/pr.md"; then
  created=1
else
  created=0
fi
# a pull request opened with GITHUB_TOKEN starts no workflow; a workflow_dispatch run is the exception
write gh workflow run ci.yml --ref "$BRANCH" || echo "could not start CI on $BRANCH" >&2
if [ "$created" = 1 ]; then
  for older in $(gh pr list --state open --limit 100 --json number,headRefName \
      --jq "map(select((.headRefName | startswith(\"app-canary/\")) and .headRefName != \"$BRANCH\")) | .[].number"); do
    write gh pr close "$older" --comment "Superseded by the app canary's pull request for $BRANCH."
  done
  exit 0
fi

echo "could not open the pull request; falling back to an issue" >&2
{
  echo "The app canary passed on $versions, but it could not open the pull request itself (Settings - Actions - General -"
  echo "\"Allow GitHub Actions to create and approve pull requests\")."
  echo "The branch is pushed and ready: [\`$BRANCH\`]($GH_SERVER/$GH_REPO/compare/main...$BRANCH?expand=1)."
  echo
  cat "$tmp/pr.md"
} > "$tmp/issue.md"
title="[app-canary] $versions is ready to be recorded"
number=$(issue_number "$title")
if [ -n "$number" ]; then
  write gh issue comment "$number" --body-file "$tmp/issue.md"
else
  write gh issue create --title "$title" --body-file "$tmp/issue.md"
fi
