"""Preflight of an integration version before anything touches the running
environment: the release is unpacked into a scratch directory, its
requirements are resolved by pip in dry-run mode against this venv (nothing
installed), every patch is evaluated against the new code and against the
requirement versions the update would bring, the manifest's dependencies
are checked against HA's loader, and the minimum Home Assistant version
(hacs.json) is compared with the target.  The report says what would
change and whether anything blocks the update.  Used by "Preflight" on the
Config page and by the environment builder."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Any

from homeassistant import loader
from homeassistant.const import __version__ as ha_version
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from jsonio import vkey

from . import patches
from .installer import _req_name

_LOGGER = logging.getLogger(__name__)
LOCK = asyncio.Lock()  # one pip resolution at a time (UI, builder, MQTT update)

PIP_TIMEOUT_S = 300
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
GITHUB_API = "https://api.github.com/repos/{repo}"




def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""

def _pip_dry_run(python: str, requirements: list[str], constraints: str | None) -> dict[str, Any]:
    """Blocking: what pip would install for ``requirements`` in this venv.
    ``--dry-run --report`` resolves everything (wheels are downloaded to a
    temporary place, nothing is installed)."""
    if not requirements:
        return {"ok": True, "install": [], "stderr": ""}
    cmd = [python, "-m", "pip", "install", "--dry-run", "--quiet", "--report", "-", *requirements]
    if constraints and os.path.isfile(constraints):
        cmd += ["-c", constraints]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_S, cwd="/tmp")
    except subprocess.TimeoutExpired:
        return {"ok": False, "install": [], "stderr": f"pip did not finish within {PIP_TIMEOUT_S}s"}
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
        return {"ok": False, "install": [], "stderr": tail}
    try:
        report = json.loads(proc.stdout or "{}")
    except ValueError:
        return {"ok": False, "install": [], "stderr": "pip report was not JSON"}
    rows = []
    for item in report.get("install", []):
        meta = item.get("metadata", {})
        rows.append({"name": meta.get("name"), "version": meta.get("version"),
                     "requested": bool(item.get("requested")), "requires_python": meta.get("requires_python")})
    return {"ok": True, "install": rows, "stderr": ""}


def _patch_after_update(text: str, new_versions: dict[str, str]) -> str:
    """What the '# applies-to:' header says once the update's requirement
    versions are installed: applies | skipped | n/a (no header) | unknown."""
    m = patches._APPLIES_RE.search(text)
    if not m:
        return "n/a"
    try:
        from packaging.requirements import Requirement

        r = Requirement(m.group(1))
    except Exception:  # noqa: BLE001
        return "unknown"
    ver = new_versions.get(r.name.lower().replace("_", "-"))
    if not ver:
        return "unknown"
    return "applies" if (not r.specifier or r.specifier.contains(ver, prereleases=True)) else "skipped"


def _config_flow_version(component_dir: str) -> int | None:
    """Blocking: VERSION of the ConfigFlow class in <component>/config_flow.py, read with ast (never imported)."""
    import ast

    try:
        with open(os.path.join(component_dir, "config_flow.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not any(k.arg == "domain" for k in node.keywords):
            continue
        for item in node.body:
            if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in item.targets):
                if isinstance(item.value, ast.Constant) and isinstance(item.value.value, int):
                    return item.value.value
        return 1  # a config flow without VERSION is version 1
    return None


async def run(hass: HomeAssistant, installer, domain: str, ref: str, target_ha: str | None = None) -> dict[str, Any]:
    t0 = time.monotonic()
    spec = installer.spec(domain)
    if not spec or not spec.get("repo"):
        raise ValueError(f"{domain}: no GitHub repository known (registry)")
    repo = spec["repo"]
    session = async_get_clientsession(hass)
    blockers: list[str] = []
    warnings: list[str] = []

    # 1. the release, into a scratch directory
    async with session.get(GITHUB_API.format(repo=repo) + f"/zipball/{ref}", headers=installer.settings.github_headers()) as resp:
        if resp.status != 200:
            raise ValueError(f"{repo}@{ref}: GitHub answered {resp.status}")
        blob = await resp.read()
    scratch = os.path.join(installer.versions_dir, domain, f".preflight-{int(time.time())}")
    try:
        manifest = await hass.async_add_executor_job(installer._unpack, blob, domain, scratch)
        new_reqs = list(manifest.get("requirements", []))

        # 2. minimum Home Assistant version (hacs.json) vs the target
        target = target_ha or ha_version
        min_ha = None
        try:
            async with session.get(RAW.format(repo=repo, ref=ref, path="hacs.json"), headers=installer.settings.github_headers()) as r2:
                if r2.status == 200:
                    min_ha = (json.loads(await r2.text()) or {}).get("homeassistant")
        except Exception as err:  # noqa: BLE001
            warnings.append(f"hacs.json could not be read: {err}")
        if min_ha and vkey(min_ha) > vkey(target):
            blockers.append(f"needs Home Assistant >= {min_ha}, target is {target}")

        # 3. dependencies: every domain the manifest names must be loadable here
        deps = list(manifest.get("dependencies", [])) + list(manifest.get("after_dependencies", []))
        dep_rows = []
        dep_reqs: list[str] = []
        for dep in deps:
            try:
                integ = await loader.async_get_integration(hass, dep)
                dep_rows.append({"domain": dep, "found": True, "requirements": list(integ.requirements or [])})
                dep_reqs.extend(integ.requirements or [])
            except loader.IntegrationNotFound:
                dep_rows.append({"domain": dep, "found": False, "requirements": []})
                if dep in manifest.get("dependencies", []):
                    blockers.append(f"dependency '{dep}' is not available in this Home Assistant")
                else:
                    warnings.append(f"after_dependency '{dep}' is not available here")

        # 4. requirements: pip dry-run against this venv
        all_reqs = list(dict.fromkeys(new_reqs + dep_reqs))
        installed_now = {_req_name(req): ver for req, ver in installer._requirement_versions(all_reqs).items()}
        pip = await hass.async_add_executor_job(_pip_dry_run, sys.executable, all_reqs, installer.constraints)
        if not pip["ok"]:
            blockers.append("requirements cannot be resolved: " + (pip["stderr"].splitlines()[-1] if pip["stderr"] else "pip failed"))
        new_versions = {str(r["name"]).lower().replace("_", "-"): str(r["version"]) for r in pip["install"] if r.get("name")}
        for name, ver in installed_now.items():
            key = name.lower().replace("_", "-")
            new_versions.setdefault(key, ver or "")
        req_rows = []
        for req in all_reqs:
            name = _req_name(req)
            key = name.lower().replace("_", "-")
            change = next((r for r in pip["install"] if str(r.get("name", "")).lower().replace("_", "-") == key), None)
            req_rows.append({"requirement": req, "installed": installed_now.get(name), "after": change["version"] if change else installed_now.get(name),
                             "action": ("install" if change and not installed_now.get(name) else "upgrade" if change else "unchanged"),
                             "from_dependency": req in dep_reqs and req not in new_reqs})
        extra = [r for r in pip["install"] if str(r.get("name", "")).lower().replace("_", "-") not in
                 {_req_name(q).lower().replace("_", "-") for q in all_reqs}]
        if target != ha_version:
            warnings.append(f"requirements were resolved against Home Assistant {ha_version} (this venv); the {target} venv is built at the restart and the requirements reinstalled there")

        # 5. patches against the new code and the new requirement versions
        rows = await hass.async_add_executor_job(
            patches.status, installer.config_dir, domain, installer.site_packages_for(domain), scratch, ref)
        patch_rows = []
        for row in rows:
            path = patches.patch_path(installer.config_dir, domain, row["name"])
            text = await hass.async_add_executor_job(_read_text, path)
            after = _patch_after_update(text, new_versions)
            patch_rows.append({**row, "after_update": after})
            st = str(row.get("status", ""))
            if st.startswith(("error", "failed")):
                blockers.append(f"patch {row['name']}: {st}")
            elif st == "not applicable" and after != "skipped":
                warnings.append(f"patch {row['name']} does not fit the new code (its context changed); it will be reported, not applied")

        # 6. configuration surface
        yaml_present = os.path.isfile(installer.yaml_path(domain))
        old = installer.installed_manifest(domain) or {}
        cfg = {"config_flow": bool(manifest.get("config_flow")), "yaml_config_present": yaml_present,
               "integration_type": manifest.get("integration_type"), "iot_class": manifest.get("iot_class"),
               "entries_here": len(installer._entries_of(domain))}
        if not manifest.get("config_flow") and not yaml_present and not cfg["entries_here"]:
            warnings.append("no config flow and no YAML stored here: the integration would start unconfigured")
        entries = installer._entries_of(domain)
        if not manifest.get("config_flow") and entries:
            warnings.append(f"{ref} has no config flow, but {len(entries)} config entr{'y exists' if len(entries) == 1 else 'ies exist'} here: "
                            "Home Assistant cannot set them up with this version (they come back when a version with a config flow runs)")
        flow_version = await hass.async_add_executor_job(_config_flow_version, scratch)
        cfg["config_flow_version"] = flow_version
        newest_entry = max((e.version for e in entries), default=None)
        if flow_version is not None and newest_entry is not None and newest_entry > flow_version:
            warnings.append(f"config entries here are at version {newest_entry}, {ref}'s config flow is version {flow_version}: "
                            "Home Assistant cannot migrate an entry back (migration_error). Full rollback right after an upgrade "
                            "brings the entry back as it was; otherwise delete the entry and set it up again")
        if manifest.get("config_flow") and yaml_present and not entries:
            warnings.append("YAML is stored here and this version has a config flow: if it imports the YAML into a config entry, "
                            "remove the YAML afterwards (it is still applied at every boot)")

        return {
            "domain": domain, "ref": ref, "repo": repo, "target_ha": target, "current_ha": ha_version,
            "ok": not blockers, "blockers": blockers, "warnings": warnings,
            "versions": {"running": old.get("version"), "running_tag": (installer.state.installed.get(domain) or {}).get("running_tag"),
                         "new": manifest.get("version"), "min_ha": min_ha},
            "requirements": req_rows, "also_installed": extra, "pip_ok": pip["ok"], "pip_error": pip["stderr"],
            "dependencies": dep_rows, "patches": patch_rows, "config": cfg,
            "duration_s": round(time.monotonic() - t0, 1),
        }
    finally:
        def _cleanup() -> None:
            shutil.rmtree(scratch, ignore_errors=True)
            try:
                os.rmdir(os.path.dirname(scratch))  # a domain that is not installed leaves no empty dir behind
            except OSError:
                pass

        await hass.async_add_executor_job(_cleanup)
