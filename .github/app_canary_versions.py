"""What the weekly app canary tests, and whether anything moved since the last review, as GITHUB_OUTPUT lines.

The app runs under the Supervisor on Home Assistant OS, and both ship on their own schedule.  .github/app_versions.json
records what was last reviewed: the stable channel's Supervisor, Home Assistant OS and Core
(https://version.home-assistant.io/stable.json), the newest release of the app linter CI pins, and a digest of the
developer documentation of the app configuration.  This asks where each of them is today:

  supervisor, haos, core     the stable channel (supervisor, hassos.ova, homeassistant.default)
  supervisor_beta            the beta channel's Supervisor
  supervisor_newest          the newest Supervisor release on GitHub (it reaches the beta channel after a while)
  linter, linter_sha         frenck/action-app-linter's newest release and its commit
  configuration_md_sha256    developers.home-assistant docs/apps/configuration.md on master

and prints them with:

  changed    true when a version moved past the record, or the linter or the documentation changed
  moved      what moved, one "; "-separated line for the report
  schema     the Supervisor refs the schema job checks the app against, a JSON list of {"kind", "ref"}: the stable
             channel's, the beta channel's (the same tag between releases: a leg each all the same, so the report
             always hears from both) and main
  proposed   the record a passing run proposes (a version never moves back), compact JSON
  branch     the pull request branch for that record
"""

import hashlib
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RECORD = os.path.join(HERE, "app_versions.json")
CHANNEL = "https://version.home-assistant.io/{}.json"
GITHUB = "https://api.github.com/repos/{}"
CONFIGURATION_MD = "https://raw.githubusercontent.com/home-assistant/developers.home-assistant/master/docs/apps/configuration.md"
VERSIONED = (("supervisor", "Supervisor"), ("haos", "Home Assistant OS"), ("core", "Home Assistant Core"))


def key(version: str) -> tuple:
    """Order of Supervisor (2026.09.3), OS (18.3, 18.4.rc1) and Core (2026.10.0b1, 2026.10.0.dev2026...) versions:
    a pre-release sorts after the release before it and before its own."""
    m = re.fullmatch(r"(\d+(?:\.\d+)*)(?:\.?(dev|a|b|rc)\.?(\d+))?", version.strip())
    if not m:
        raise ValueError(f"not a version: {version!r}")
    release = tuple(int(p) for p in m.group(1).split("."))
    release += (0,) * (4 - len(release))
    pre = {"dev": 0, "a": 1, "b": 2, "rc": 3}.get(m.group(2), 4)
    return (*release, pre, int(m.group(3) or 0))


def newer(a: str, b: str) -> bool:
    return bool(a) and (not b or key(a) > key(b))


def decide(record: dict, now: dict) -> dict:
    moved, proposed = [], dict(record)
    for field, label in VERSIONED:
        if newer(now[field], record.get(field, "")):
            moved.append(f"{label} {record.get(field) or 'none'} -> {now[field]}")
            proposed[field] = now[field]
    if now["linter_sha"] != record.get("linter_sha"):
        moved.append(f"app linter {record.get('linter') or 'none'} -> {now['linter']}")
        proposed["linter"], proposed["linter_sha"] = now["linter"], now["linter_sha"]
    if now["configuration_md_sha256"] != record.get("configuration_md_sha256"):
        moved.append("the developer documentation of the app configuration changed")
        proposed["configuration_md_sha256"] = now["configuration_md_sha256"]
    schema = [{"kind": "stable", "ref": now["supervisor"]}, {"kind": "beta", "ref": now["supervisor_beta"]},
              {"kind": "main", "ref": "main"}]
    branch = "app-canary/sup-{supervisor}-os-{haos}-core-{core}-lint-{linter}-docs-{docs}".format(
        docs=proposed["configuration_md_sha256"][:7], **proposed)
    return {
        "changed": "true" if moved else "false",
        "moved": "; ".join(moved),
        "schema": json.dumps(schema, separators=(",", ":")),
        "proposed": json.dumps(proposed, separators=(",", ":")),
        "branch": branch,
    }


def _get(url: str) -> bytes:
    headers = {"User-Agent": "hass-remote-integration app canary"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        return resp.read()


def _json(url: str):
    return json.loads(_get(url))


def fetch() -> dict:
    stable, beta = _json(CHANNEL.format("stable")), _json(CHANNEL.format("beta"))
    releases = _json(GITHUB.format("home-assistant/supervisor/releases?per_page=30"))
    tags = [r["tag_name"] for r in releases if not r.get("draft") and re.fullmatch(r"\d+\.\d+\.\d+", r["tag_name"])]
    linter = _json(GITHUB.format("frenck/action-app-linter/releases/latest"))["tag_name"]
    return {
        "supervisor": stable["supervisor"],
        "haos": stable["hassos"]["ova"],
        "core": stable["homeassistant"]["default"],
        "supervisor_beta": beta["supervisor"],
        "supervisor_newest": max(tags, key=key, default=""),
        "linter": linter,
        "linter_sha": _json(GITHUB.format(f"frenck/action-app-linter/commits/{linter}"))["sha"],
        "configuration_md_sha256": hashlib.sha256(_get(CONFIGURATION_MD)).hexdigest(),
    }


def main() -> None:
    with open(RECORD, encoding="utf-8") as fh:
        record = json.load(fh)
    now = fetch()
    out = {**{k: v for k, v in now.items()}, **decide(record, now)}
    for name, value in out.items():
        print(f"{name}={value}")
    for name, value in out.items():  # the log, for a person reading the run
        print(f"{name}: {value}", file=sys.stderr)


if __name__ == "__main__":
    main()
