#!/bin/sh
# What the weekly canary does with its result.  A failure is something to investigate, so it becomes an
# issue (one per pair of versions, commented on rather than repeated).  A pass on a stable release newer
# than the one the image installs is something to approve, so it becomes a pull request that moves
# HA_VERSION_DEFAULT there - never HA_VERSION_MIN, which is a measured floor and not a moving target.
# Reads RESULT (the boot job's conclusion), STABLE, PRERELEASE, DEFAULT, RUN and GH_TOKEN.
set -eu

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

if [ "$RESULT" = "failure" ]; then
  title="[canary] Home Assistant $STABLE${PRERELEASE:+ / $PRERELEASE} breaks the manager"
  cat > "$tmp/body.md" <<EOF
The weekly canary failed.

| | |
|---|---|
| newest stable | \`$STABLE\` |
| newest pre-release | \`${PRERELEASE:-none}\` |
| this image installs | \`$DEFAULT\` |

[The run]($RUN) says which job and which step. A failure here means that on one of those Home Assistant
versions the image no longer boots, the manager no longer answers, its discovery payloads no longer
validate against the MQTT schemas of that version, or the unit suite no longer passes there.
EOF
  number=$(gh issue list --state open --search "canary in:title" --json number,title \
    --jq "map(select(.title == \"$title\")) | .[0].number // empty")
  if [ -n "$number" ]; then
    gh issue comment "$number" --body-file "$tmp/body.md"
  else
    gh issue create --title "$title" --body-file "$tmp/body.md"
  fi
  exit 0
fi

if [ "$RESULT" != "success" ]; then
  echo "the boot job was $RESULT: nothing to report"
  exit 0
fi
if [ "$STABLE" = "$DEFAULT" ]; then
  echo "the image already installs $DEFAULT"
  exit 0
fi
branch="canary/ha-$STABLE"
open=$(gh pr list --head "$branch" --state open --json number --jq 'length')
if [ "$open" != "0" ]; then
  echo "a pull request for $STABLE is already open"
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git checkout -q -b "$branch"
sed -i "s/^ARG HA_VERSION=.*/ARG HA_VERSION=$STABLE/" Dockerfile
if git diff --quiet; then
  echo "the Dockerfile already asks for $STABLE"
  exit 0
fi

cat > "$tmp/commit.txt" <<EOF
install Home Assistant $STABLE on a fresh volume

The weekly canary booted the image on $STABLE and ran the discovery schemas and
the unit suite there.  HA_VERSION_DEFAULT is what a fresh volume installs when
it is told not to take the newest, and the fallback when PyPI cannot be reached,
so it should be a version something has actually started.
EOF
cat > "$tmp/pr.md" <<EOF
The canary booted the image on Home Assistant **$STABLE** and it passed: the manager answered on that
version, the discovery payloads validated against its MQTT schemas, and the unit suite ran there.

This moves \`HA_VERSION_DEFAULT\` from \`$DEFAULT\` to \`$STABLE\` - what a fresh volume installs when it
is told not to take the newest, and the fallback when PyPI cannot be reached. It leaves
\`HA_VERSION_MIN\` alone: the oldest version this image installs is a measurement, and it moves only when
somebody measures again.

[The canary run]($RUN).
EOF
git commit -qaF "$tmp/commit.txt"
git push -q origin "$branch"
gh pr create --head "$branch" --base main --title "install Home Assistant $STABLE on a fresh volume" --body-file "$tmp/pr.md"
