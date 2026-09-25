"""Release notes of the Supervisor and Home Assistant OS since the versions last reviewed, reduced to what may concern
the app, as a markdown issue body.

    python .github/app_release_scan.py <body.md> [<configuration.md sha256 today>]

Reads .github/app_versions.json for the reviewed versions, lists the releases of home-assistant/supervisor and
home-assistant/operating-system newer than those with `gh api` (pre-releases too: they are the early warning), and
keeps from each body its "Breaking Changes" sections whole, and from the rest the lines that mention an app-relevant
word (KEYWORDS; dependency bumps left out).  When the digest of the developer documentation of the app configuration
differs from the recorded one it lists the commits that touched that page.

Prints found=true|false (GITHUB_OUTPUT) and writes the body only when something was found.  The body starts with a
marker naming what it covers, so the report can tell whether it already said this.
"""

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RECORD = os.path.join(HERE, "app_versions.json")
KEYWORDS = re.compile(
    r"app config|addon|app_config|map|backup|restore|exclude|uart|usb|device|capabilit|NET_RAW|privileged|apparmor"
    r"|rating|ingress|port|deprecat|legacy|remov|drop|schema|options|image|docker|containerd|arch",
    re.IGNORECASE,
)
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
REPOS = (("home-assistant/supervisor", "supervisor", "Supervisor"),
         ("home-assistant/operating-system", "haos", "Home Assistant OS"))
DOCS_REPO = "home-assistant/developers.home-assistant"
DOCS_PATH = "docs/apps/configuration.md"


def key(version: str) -> tuple:
    m = re.fullmatch(r"(\d+(?:\.\d+)*)(?:\.?(dev|a|b|rc)\.?(\d+))?", version.strip())
    if not m:
        return ()
    release = tuple(int(p) for p in m.group(1).split("."))
    release += (0,) * (4 - len(release))
    return (*release, {"dev": 0, "a": 1, "b": 2, "rc": 3}.get(m.group(2), 4), int(m.group(3) or 0))


def sections(body: str) -> tuple[list[str], list[str]]:
    """(the lines of every Breaking Changes section, the other lines worth a look)."""
    breaking, hits = [], []
    in_breaking = in_deps = None  # the level of the heading that opened the section
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        m = HEADING.match(line)
        if m:
            level = len(m.group(1))
            if in_breaking is not None and level <= in_breaking:
                in_breaking = None
            if in_deps is not None and level <= in_deps:
                in_deps = None
            if re.search(r"breaking", m.group(2), re.IGNORECASE):
                in_breaking = level
            elif re.search(r"dependenc", m.group(2), re.IGNORECASE):
                in_deps = level
            continue
        text = line.strip()
        if not text or text.startswith(("<details", "</details", "<summary")):
            continue
        if in_breaking is not None:
            breaking.append(text)
        elif in_deps is None and KEYWORDS.search(text):
            hits.append(text)
    return breaking, hits


def newer_releases(releases: list[dict], since: str) -> list[dict]:
    floor = key(since)
    out = [r for r in releases if not r.get("draft") and key(r["tag_name"]) and key(r["tag_name"]) > floor]
    return sorted(out, key=lambda r: key(r["tag_name"]))


def scan(record: dict, releases: dict, docs_sha: str = "", docs_commits: list | None = None) -> tuple[bool, str]:
    """``releases`` maps "home-assistant/supervisor" and "home-assistant/operating-system" to GitHub release objects."""
    parts, covered = [], []
    for repo, field, label in REPOS:
        for rel in newer_releases(releases.get(repo, []), record.get(field, "")):
            breaking, hits = sections(rel.get("body", ""))
            if not breaking and not hits:
                continue
            tag = rel["tag_name"]
            covered.append(f"{field} {tag}")
            pre = " (pre-release)" if rel.get("prerelease") else ""
            parts.append(f"## [{label} {tag}]({rel.get('html_url') or f'https://github.com/{repo}/releases/tag/{tag}'}){pre}\n")
            if breaking:
                parts.append("**Breaking changes**\n\n" + "\n".join(breaking) + "\n")
            if hits:
                parts.append("**Lines that may concern the app**\n\n" + "\n".join(
                    line if line.startswith(("-", "*")) else f"- {line}" for line in hits) + "\n")
    if docs_sha and docs_sha != record.get("configuration_md_sha256"):
        covered.append(f"docs {docs_sha[:7]}")
        lines = [f"## [{DOCS_PATH}](https://github.com/{DOCS_REPO}/blob/master/{DOCS_PATH}) changed\n",
                 f"sha256 `{record.get('configuration_md_sha256', '')[:12]}` -> `{docs_sha[:12]}`. The latest commits "
                 "that touched it:\n"]
        for c in (docs_commits or [])[:10]:
            first = (c.get("commit", {}).get("message") or "").splitlines()[0] if c.get("commit") else ""
            date = c.get("commit", {}).get("committer", {}).get("date", "")[:10]
            lines.append(f"- {date} [{first or c.get('sha', '')[:7]}]({c.get('html_url', '')})")
        parts.append("\n".join(lines) + "\n")
    if not parts:
        return False, ""
    marker = f"<!-- app-canary-notes: {', '.join(covered)} -->"
    head = (f"{marker}\nRelease notes since the versions recorded in `.github/app_versions.json` that may concern the "
            "app: every Breaking Changes section, and the other lines that mention an app-relevant word.\n")
    return True, head + "\n" + "\n".join(parts)


def _gh(path: str):
    out = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.split("\n\n", 2)[1], file=sys.stderr)
        return 2
    with open(RECORD, encoding="utf-8") as fh:
        record = json.load(fh)
    releases = {repo: _gh(f"repos/{repo}/releases?per_page=50") for repo, _, _ in REPOS}
    docs_sha = argv[2] if len(argv) == 3 else ""
    commits = _gh(f"repos/{DOCS_REPO}/commits?path={DOCS_PATH}&per_page=10") if docs_sha and docs_sha != record.get(
        "configuration_md_sha256") else []
    found, body = scan(record, releases, docs_sha, commits)
    if found:
        with open(argv[1], "w", encoding="utf-8") as fh:
            fh.write(body)
        print(body, file=sys.stderr)
    else:
        print("nothing newer than the record mentions the app", file=sys.stderr)
    print(f"found={'true' if found else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
