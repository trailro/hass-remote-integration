#!/bin/sh
# What the weekly canary does with its result.  A failure is something to investigate, so it becomes an
# issue (one per pair of versions, commented on rather than repeated).  A pass on a stable release newer
# than the one the image installs is something to approve, so it becomes a pull request that moves
# HA_VERSION_DEFAULT there - never HA_VERSION_MIN, which is a measured floor and not a moving target.  The two
# are decided apart: a pre-release that breaks the manager is an issue, and the stable release that passed is still
# proposed.  One pull request per version, ever: an open one, or one closed without merging, is not opened again; a
# newer one closes the older canary pull requests still open.
# Reads STABLE_RESULT and PRERELEASE_RESULT (each boot leg's conclusion), STABLE, PRERELEASE, DEFAULT, RUN,
# GH_SERVER, GH_REPO and GH_TOKEN.
set -eu

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

broken=""
[ "$STABLE_RESULT" = failure ] && broken="$STABLE"
[ "$PRERELEASE_RESULT" = failure ] && [ -n "$PRERELEASE" ] && broken="${broken:+$broken / }$PRERELEASE"
if [ -n "$broken" ]; then
  title="[canary] Home Assistant $broken breaks the manager"
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
fi

if [ "$STABLE_RESULT" != "success" ]; then
  echo "the stable leg was $STABLE_RESULT: nothing to propose"
  exit 0
fi
if [ "$STABLE" = "$DEFAULT" ]; then
  echo "the image already installs $DEFAULT"
  exit 0
fi
branch="canary/ha-$STABLE"
states=$(gh pr list --head "$branch" --state all --json state --jq 'map(.state) | join(" ")')
case " $states " in
  *" OPEN "*) echo "a pull request for $STABLE is already open"; exit 0 ;;
  *" CLOSED "*) echo "a pull request for $STABLE was closed without merging: not proposing it again"; exit 0 ;;
esac

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
if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
  # week two with the same newest release: the branch is already there, carrying the same one-line change.
  # Pushing again would be rejected as non-fast-forward and, under set -e, would end the run before the
  # report below - so the canary would go quiet exactly when it has something to say.
  echo "the branch for $STABLE is already on the remote; not pushing again"
else
  git push -q origin "$branch"
fi
if gh pr create --head "$branch" --base main --title "install Home Assistant $STABLE on a fresh volume" --body-file "$tmp/pr.md"; then
  created=1
else
  created=0
fi
# a pull request opened with GITHUB_TOKEN starts no workflow; a workflow_dispatch run is the exception
gh workflow run ci.yml --ref "$branch" || echo "could not start CI on $branch" >&2
if [ "$created" = 1 ]; then
  for older in $(gh pr list --state open --limit 100 --json number,headRefName \
      --jq "map(select((.headRefName | startswith(\"canary/ha-\")) and .headRefName != \"$branch\")) | .[].number"); do
    gh pr close "$older" --comment "Superseded by the canary's pull request for Home Assistant $STABLE."
  done
  exit 0
fi

# "GitHub Actions is not permitted to create or approve pull requests" is a repository setting, off by
# default (Settings - Actions - General).  The branch is pushed either way, so say where it is instead of
# failing the run: a canary that reports nothing because of a permission is worse than one that reports
# by hand.
echo "could not open the pull request; falling back to an issue" >&2
{
  echo "The canary booted the image on Home Assistant **$STABLE** and it passed, but it could not open the pull request itself:"
  echo
  echo "> GitHub Actions is not permitted to create or approve pull requests"
  echo
  echo "That is a repository setting (Settings - Actions - General - \"Allow GitHub Actions to create and approve pull requests\")."
  echo "The branch is pushed and ready: [\`$branch\`]($GH_SERVER/$GH_REPO/compare/main...$branch?expand=1)."
  echo
  cat "$tmp/pr.md"
} > "$tmp/issue.md"
title="[canary] Home Assistant $STABLE is ready to become the default"
number=$(gh issue list --state open --search "canary in:title" --json number,title \
  --jq "map(select(.title == \"$title\")) | .[0].number // empty")
if [ -n "$number" ]; then
  gh issue comment "$number" --body-file "$tmp/issue.md"
else
  gh issue create --title "$title" --body-file "$tmp/issue.md"
fi
