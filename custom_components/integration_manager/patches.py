"""User-uploaded patches, per integration: ``<config>/integration_manager/patches/<domain>/``.

Two formats, applied in file-name order after the requirements are
installed (install, update, every boot's reconcile):

* ``*.py`` - a patch module with ``apply(ctx) -> str`` and
  ``status(ctx) -> str`` (return one of "applied", "already applied",
  "absent", "not applicable", or a free-text failure).  ``ctx`` is a
  :class:`PatchContext` (site_packages, component_dir, config_dir, domain).
  Modules are the robust choice: they can locate a function by pattern
  instead of by line number, so they survive upstream refactors.
* ``*.patch`` - a unified diff.  Paths are resolved against the component
  dir first, then site-packages (``a/some_lib/module.py`` → strip one
  segment).  Applied only when every hunk's context matches; if the
  "after" text is already there it reports "already applied"; otherwise
  "not applicable" and nothing is written.

An optional header line ``# applies-to: <pip requirement>`` (e.g.
``# applies-to: some-lib<2.0``) makes a patch skip itself once the
installed distribution no longer matches, so a patch retires quietly when
upstream ships the fix.  Failures never block the integration.
"""

from __future__ import annotations

import difflib
import importlib.metadata as md
import importlib.util
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

PATCH_DIR = os.path.join("integration_manager", "patches")
BUNDLED_DIR = os.environ.get("HRI_BUNDLED_PATCHES", "/app/patches")  # shipped with the image, read-only
_APPLIES_RE = re.compile(r"^\s*#\s*applies-to:\s*(.+?)\s*$", re.M)
_VERSION_RE = re.compile(r"^\s*#\s*integration-version:\s*(.+?)\s*$", re.M)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(py|patch)$")


@dataclass
class PatchContext:
    config_dir: str
    domain: str
    site_packages: str
    component_dir: str


def patch_dir(config_dir: str, domain: str) -> str:
    return os.path.join(config_dir, PATCH_DIR, domain)


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and ".." not in name


def validate(name: str, text: str) -> str | None:
    """Why ``text`` cannot be stored as patch ``name``, or None."""
    if not valid_name(name):
        return "file must be <name>.py or <name>.patch"
    if name.endswith(".py"):
        try:
            compile(text, name, "exec")
        except SyntaxError as err:
            return f"not valid Python: {err}"
        if "def apply(" not in text or "def status(" not in text:
            return "a .py patch must define apply(ctx) and status(ctx)"
    elif not parse_unified(text):
        return "no hunks found: not a unified diff"
    return None


def bundled_dir(domain: str) -> str:
    return os.path.join(BUNDLED_DIR, domain)


def patch_path(config_dir: str, domain: str, name: str) -> str:
    """A user file on the volume wins over a bundled file of the same name."""
    user = os.path.join(patch_dir(config_dir, domain), name)
    return user if os.path.isfile(user) else os.path.join(bundled_dir(domain), name)


def is_bundled(config_dir: str, domain: str, name: str) -> bool:
    return not os.path.isfile(os.path.join(patch_dir(config_dir, domain), name)) and os.path.isfile(os.path.join(bundled_dir(domain), name))


def list_patches(config_dir: str, domain: str) -> list[str]:
    names: set[str] = set()
    for d in (bundled_dir(domain), patch_dir(config_dir, domain)):
        if os.path.isdir(d):
            names.update(n for n in os.listdir(d) if valid_name(n))
    return sorted(names)


def version_scope(text: str) -> list[str] | None:
    """Tags listed in '# integration-version: a, b' (None = every version)."""
    m = _VERSION_RE.search(text)
    if not m or m.group(1).strip().lower() in ("all", "*", "any"):
        return None
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


def _applies(text: str, running_tag: str | None = None) -> tuple[bool, str]:
    """(applies, reason) from the optional '# applies-to:' (pip requirement)
    and '# integration-version:' (tags) headers."""
    scope = version_scope(text)
    if scope is not None and running_tag not in scope:
        return False, f"scoped to version(s) {', '.join(scope)}, running {running_tag or 'none'}"
    m = _APPLIES_RE.search(text)
    if not m:
        return True, ""
    req = m.group(1)
    try:
        from packaging.requirements import Requirement

        r = Requirement(req)
        installed = md.version(r.name)
    except md.PackageNotFoundError:
        return False, f"{req}: not installed"
    except Exception as err:  # noqa: BLE001
        return True, f"bad applies-to ({err}), applying anyway"
    if r.specifier and not r.specifier.contains(installed, prereleases=True):
        return False, f"installed {r.name} {installed} is outside {r.specifier}"
    return True, ""


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _load_module(path: str):
    name = "user_patch_" + re.sub(r"\W", "_", os.path.basename(path))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses and typing resolve the module through sys.modules while it executes
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    finally:
        sys.modules.pop(name, None)
    return mod


def _run(config_dir: str, domain: str, site_packages: str, component_dir: str, running_tag: str | None, apply: bool) -> list[dict[str, Any]]:
    """status() and apply_all() share everything but the verb."""
    ctx = PatchContext(config_dir, domain, site_packages, component_dir)
    out = []
    for name in list_patches(config_dir, domain):
        path = patch_path(config_dir, domain, name)
        bundled = is_bundled(config_dir, domain, name)
        try:
            text = _read_text(path)
            scope = version_scope(text)
            applies, why = _applies(text, running_tag)
            if not applies:
                out.append({"name": name, "status": "skipped", "detail": why, "scope": scope, "bundled": bundled})
                continue
            if name.endswith(".py"):
                mod = _load_module(path)
                st = str(mod.apply(ctx) if apply else mod.status(ctx))
            else:
                st = _diff_apply(text, ctx) if apply else _diff_status(text, ctx)
            out.append({"name": name, "status": st, "detail": why, "scope": scope, "bundled": bundled})
        except Exception as err:  # noqa: BLE001
            if apply:
                _LOGGER.exception("patch %s failed", name)
            out.append({"name": name, "status": f"{'failed' if apply else 'error'}: {type(err).__name__}: {err}", "detail": "", "bundled": bundled})
    return out


def status(config_dir: str, domain: str, site_packages: str, component_dir: str, running_tag: str | None = None) -> list[dict[str, Any]]:
    return _run(config_dir, domain, site_packages, component_dir, running_tag, apply=False)


def apply_all(config_dir: str, domain: str, site_packages: str, component_dir: str, running_tag: str | None = None) -> list[dict[str, Any]]:
    return _run(config_dir, domain, site_packages, component_dir, running_tag, apply=True)


def check(config_dir: str, domain: str, site_packages: str, component_dir: str, running_tag: str | None, name: str, text: str) -> dict[str, Any]:
    """Dry run of a patch that may not be saved yet, against the deployed
    code; nothing is written.  A ``.patch`` reports every hunk (applied,
    pending, not applicable) and, for a hunk whose context is gone, the
    closest lines in the file and how they differ from what the hunk
    expects.  A ``.py`` module is loaded from a temporary copy and its
    ``status(ctx)`` is reported."""
    if (err := validate(name, text)):
        return {"ok": False, "error": err}
    ctx = PatchContext(config_dir, domain, site_packages, component_dir)
    applies, why = _applies(text, running_tag)
    out: dict[str, Any] = {"ok": True, "name": name, "scope": version_scope(text), "applies": applies, "detail": why,
                           "against": f"{domain} {running_tag}" if running_tag else f"the deployed files of {domain}"}
    try:
        if name.endswith(".patch"):
            out["files"] = _hunk_report(text, ctx)
            status_ = _diff_status(text, ctx)
        else:
            tmp = tempfile.mkdtemp(prefix="hri-patch-check-")
            try:
                path = os.path.join(tmp, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                status_ = str(_load_module(path).status(ctx))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    except Exception as err:  # noqa: BLE001 - the user's module or an unreadable target
        status_ = f"error: {type(err).__name__}: {err}"
    out["status"] = status_ if applies else "skipped"
    out["status_if_applied"] = status_
    return out


def _display_path(target: str, ctx: PatchContext) -> str:
    for root, label in ((ctx.component_dir, f"custom_components/{ctx.domain}"), (ctx.site_packages, "site-packages")):
        if target.startswith(root.rstrip(os.sep) + os.sep):
            return f"{label}/{os.path.relpath(target, root)}"
    return target


def _closest(lines: list[str], needle: list[str], hint: int) -> int:
    """Start of the window of ``lines`` that shares the most lines, position
    by position, with ``needle`` (the nearest to ``hint`` on a tie)."""
    n, best, best_score = len(needle), 0, -1
    for i in range(0, max(1, len(lines) - n + 1)):
        score = sum(1 for k in range(min(n, len(lines) - i)) if lines[i + k] == needle[k])
        if score > best_score or (score == best_score and abs(i - hint) < abs(best - hint)):
            best, best_score = i, score
    return best


def _hunk_report(text: str, ctx: PatchContext) -> list[dict[str, Any]]:
    files = []
    for fp in parse_unified(text):
        target = _resolve(fp.path, ctx)
        row: dict[str, Any] = {"path": fp.path, "target": _display_path(target, ctx) if target else None, "hunks": []}
        files.append(row)
        if target is None:
            continue
        with open(target, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
        for h in fp.hunks:
            hint = h.old_start - 1
            hr: dict[str, Any] = {"header": f"@@ -{h.old_start},{h.old_n} @@", "state": "not applicable", "line": None}
            if (at := _find(lines, h.new_lines, hint)) >= 0:
                hr.update(state="applied", line=at + 1)
            elif (at := _find(lines, h.old_lines, hint)) >= 0:
                hr.update(state="pending", line=at + 1)
            elif h.old_lines:
                at = _closest(lines, h.old_lines, hint)
                found = lines[at:at + len(h.old_lines)]
                hr["line"] = at + 1
                hr["found_diff"] = [d for d in difflib.unified_diff(h.old_lines, found, lineterm="", n=len(h.old_lines))
                                    if not d.startswith(("---", "+++", "@@"))]
            row["hunks"].append(hr)
    return files


# ----- unified diff support ---------------------------------------------------


@dataclass
class _Hunk:
    old_start: int
    old_lines: list[str]  # context + removed
    new_lines: list[str]  # context + added
    old_n: int = 0  # counts from the @@ header: a hunk is complete when reached
    new_n: int = 0

    @property
    def complete(self) -> bool:
        return len(self.old_lines) >= self.old_n and len(self.new_lines) >= self.new_n


@dataclass
class _FilePatch:
    path: str
    hunks: list[_Hunk]


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified(text: str) -> list[_FilePatch]:
    files: list[_FilePatch] = []
    cur: _FilePatch | None = None
    hunk: _Hunk | None = None
    for line in text.splitlines():
        in_hunk = hunk is not None and not hunk.complete
        if line.startswith("--- ") and not in_hunk:
            continue
        if line.startswith("+++ ") and not in_hunk:
            path = line[4:].split("\t")[0].strip()
            cur = _FilePatch(path=path, hunks=[])
            files.append(cur)
            hunk = None
            continue
        m = _HUNK_RE.match(line)
        if m and cur is not None:
            hunk = _Hunk(old_start=int(m.group(1)), old_lines=[], new_lines=[],
                         old_n=int(m.group(2) or 1), new_n=int(m.group(4) or 1))
            cur.hunks.append(hunk)
            continue
        if hunk is None or hunk.complete:
            continue  # between hunks or files (a blank line after the last hunk is not context)
        if line.startswith("+"):
            hunk.new_lines.append(line[1:])
        elif line.startswith("-"):
            hunk.old_lines.append(line[1:])
        elif line.startswith(" ") or line == "":
            hunk.old_lines.append(line[1:] if line else "")
            hunk.new_lines.append(line[1:] if line else "")
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file"
    return [f for f in files if f.hunks]


def _resolve(path: str, ctx: PatchContext) -> str | None:
    rel = path
    for prefix in ("b/", "a/", "./"):
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
    if ".." in rel.split("/") or rel.startswith("/"):
        return None
    for root in (ctx.component_dir, ctx.site_packages):
        cand = os.path.join(root, rel)
        if os.path.isfile(cand):
            return cand
        # "custom_components/<domain>/x.py" style paths
        if rel.startswith(f"custom_components/{ctx.domain}/"):
            cand = os.path.join(ctx.component_dir, rel.split("/", 2)[2])
            if os.path.isfile(cand):
                return cand
    return None


def _find(lines: list[str], needle: list[str], hint: int) -> int:
    """Index where `needle` occurs in `lines`, nearest to `hint`, else -1."""
    n = len(needle)
    if n == 0:
        return -1
    best = -1
    for i in range(0, len(lines) - n + 1):
        if lines[i:i + n] == needle and (best == -1 or abs(i - hint) < abs(best - hint)):
            best = i
    return best


def _diff_status(text: str, ctx: PatchContext) -> str:
    states = []
    for fp in parse_unified(text):
        target = _resolve(fp.path, ctx)
        if target is None:
            return f"absent ({fp.path} not found)"
        lines = _read_text(target).split("\n")
        for h in fp.hunks:
            if _find(lines, h.new_lines, h.old_start - 1) >= 0:
                states.append("applied")
            elif _find(lines, h.old_lines, h.old_start - 1) >= 0:
                states.append("pending")
            else:
                states.append("not applicable")
    if not states:
        return "empty"
    if all(s == "applied" for s in states):
        return "applied"
    if all(s in ("applied", "pending") for s in states):
        return "pending"
    return "not applicable"


def _diff_apply(text: str, ctx: PatchContext) -> str:
    st = _diff_status(text, ctx)
    if st != "pending":
        return "already applied" if st == "applied" else st
    for fp in parse_unified(text):
        target = _resolve(fp.path, ctx)
        lines = _read_text(target).split("\n")
        for h in fp.hunks:
            if _find(lines, h.new_lines, h.old_start - 1) >= 0:
                continue
            i = _find(lines, h.old_lines, h.old_start - 1)
            if i < 0:
                return f"not applicable (context changed in {fp.path})"
            lines[i:i + len(h.old_lines)] = h.new_lines
        if target.endswith(".py"):
            compile("\n".join(lines), target, "exec")  # never leave broken Python behind
        with open(target + ".tmp", "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        os.replace(target + ".tmp", target)
    return "applied"
