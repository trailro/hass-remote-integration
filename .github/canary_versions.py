"""What the weekly canary should test, as GITHUB_OUTPUT lines.

stable:     the newest stable Home Assistant on PyPI
prerelease: the newest pre-release, but only when it is newer than that stable one (there is
            usually none between releases, and testing an old beta says nothing)
default:    what this image installs on a fresh volume today (HA_VERSION_DEFAULT in the Dockerfile),
            which is what a passing stable run would propose to move
"""

import json
import os
import re
import urllib.request

STABLE = re.compile(r"^\d{4}\.\d{1,2}\.\d+\Z")  # \Z: "$" also matches before a trailing newline


def key(version: str) -> tuple:
    # 2026.10.0b3 sorts before 2026.10.0, and after 2026.9.x: the same order entrypoint.py uses
    main, _, pre = version.partition("b")
    parts = [int(p) for p in main.split(".")]
    return (*parts, 0 if pre else 1, int(pre or 0))


def main() -> None:
    with urllib.request.urlopen("https://pypi.org/pypi/homeassistant/json", timeout=60) as resp:
        data = json.load(resp)
    usable = [v for v, files in data["releases"].items() if any(not f.get("yanked") for f in files)]
    stable = max((v for v in usable if STABLE.match(v)), key=key, default="")
    pres = [v for v in usable if not STABLE.match(v) and re.match(r"^\d{4}\.\d{1,2}\.\d+b\d+\Z", v)]
    newest_pre = max(pres, key=key, default="")
    prerelease = newest_pre if newest_pre and stable and key(newest_pre) > key(stable) else ""
    default = ""
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Dockerfile"), encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("ARG HA_VERSION="):
                default = line.split("=", 1)[1].strip()
    for name, value in (("stable", stable), ("prerelease", prerelease), ("default", default)):
        print(f"{name}={value}")


if __name__ == "__main__":
    main()
