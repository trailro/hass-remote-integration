"""Stand-ins for `gh` and `git` for the workflow scripts' tests: they answer from a JSON state file and write every
call to a log, so a test runs the real script (not a copy) and reads what it would have done on GitHub.

`gh` evaluates the few `--jq` expressions the scripts use (the test container has no jq); any other expression is an
error, so a script that starts using a new one fails its test instead of passing on a made-up answer.
"""

import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile

GH = r'''#!/usr/bin/env python3
import json, os, re, sys

state_path = os.environ["STUB_STATE"]
with open(state_path, encoding="utf-8") as fh:
    state = json.load(fh)
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(["gh", *args]) + "\n")


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def jq(expr, data):
    if expr is None:
        return json.dumps(data)
    if expr == "length":
        return str(len(data))
    if expr == 'map(.state) | join(" ")':
        return " ".join(d["state"] for d in data)
    m = re.fullmatch(r'map\(select\(\.title == "(.*)"\)\) \| \.\[0\]\.number // empty', expr)
    if m:
        hit = [d for d in data if d["title"] == m.group(1)]
        return str(hit[0]["number"]) if hit else ""
    m = re.fullmatch(r'map\(select\(\(\.headRefName \| startswith\("(.*)"\)\) and \.headRefName != "(.*)"\)\) \| \.\[\]\.number', expr)
    if m:
        return "\n".join(str(d["number"]) for d in data
                         if d["headRefName"].startswith(m.group(1)) and d["headRefName"] != m.group(2))
    if expr == '[.body] + [.comments[].body] | join("\\n")':
        return "\n".join([data.get("body", "")] + [c["body"] for c in data.get("comments", [])])
    sys.exit(f"gh stub: no answer for --jq {expr!r}")


def emit(text):
    if text:
        print(text)


STATES = {"open": {"OPEN"}, "closed": {"CLOSED", "MERGED"}, "merged": {"MERGED"}, "all": {"OPEN", "CLOSED", "MERGED"}}
cmd = " ".join(args[:2])
if cmd == "release list":
    emit(jq(opt("--jq"), state.get("releases", [])))
elif cmd == "issue list":
    want = {s.lower() for s in STATES[opt("--state", "open")]}
    emit(jq(opt("--jq"), [i for i in state.get("issues", []) if i.get("state", "OPEN").lower() in want]))
elif cmd == "issue view":
    issue = next(i for i in state.get("issues", []) if str(i["number"]) == args[2])
    emit(jq(opt("--jq"), issue))
elif cmd == "pr list":
    prs = [p for p in state.get("prs", []) if p["state"] in STATES[opt("--state", "open")]]
    if opt("--head"):
        prs = [p for p in prs if p["headRefName"] == opt("--head")]
    emit(jq(opt("--jq"), prs))
elif cmd == "pr create":
    sys.exit(state.get("pr_create_rc", 0))
elif cmd in ("issue create", "issue comment", "pr close", "workflow run"):
    pass
else:
    sys.exit(f"gh stub: no answer for {args}")
'''

GIT = r'''#!/usr/bin/env python3
import json, os, sys

with open(os.environ["STUB_STATE"], encoding="utf-8") as fh:
    state = json.load(fh)
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(["git", *args]) + "\n")
if args[:1] == ["ls-remote"]:
    sys.exit(0 if args[-1] in state.get("remote_branches", []) else 2)
if args[:1] == ["diff"]:
    sys.exit(0 if state.get("unchanged") else 1)  # --quiet: 1 when the tree changed
'''


class Stubs:
    """A bin folder with `gh` (and `git`, with ``git=True``) ahead of the real ones on PATH."""

    def __init__(self, case, state: dict, git: bool = False):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        case.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "bin").mkdir()
        tools = {"gh": GH, **({"git": GIT} if git else {})}
        python = sys.executable if os.path.basename(sys.executable).startswith("python") else "/usr/bin/env python3"
        for name, source in tools.items():
            path = self.dir / "bin" / name
            path.write_text(source.replace("#!/usr/bin/env python3", f"#!{python}", 1), encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.state_path = self.dir / "state.json"
        self.log_path = self.dir / "log.jsonl"
        self.log_path.write_text("", encoding="utf-8")
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def env(self, **extra) -> dict:
        return {**os.environ, "PATH": f"{self.dir / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
                "STUB_STATE": str(self.state_path), "STUB_LOG": str(self.log_path), **extra}

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line]

    def called(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls() if c[:len(prefix)] == list(prefix)]
