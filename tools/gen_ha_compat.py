#!/usr/bin/env python3
"""Generate ``custom_components/integration_manager/ha_compat.json``.

The manager publishes MQTT discovery into the operator's *main* Home Assistant,
which is usually not the version running in this container.  Home Assistant
drops unknown keys from a discovery payload silently, but an unknown *platform*
or an unknown *device class* fails validation and it then rejects the whole
device payload: every entity of that device disappears there, explained only by
one line in its own log.  ``date``/``time``/``datetime`` got MQTT platforms in
2026.5; ``SensorDeviceClass.RADON`` arrived in 2026.8; the next one will arrive
in some release nobody has written yet.

So the manager needs a table of *when* each platform and each device class
appeared, and that table has to be generated, not typed: a hand-written one
rots at the next Home Assistant release.  This script downloads one Home
Assistant wheel per minor release and reads out of each wheel, without
importing it:

* the MQTT entity platforms, from ``ENTITY_PLATFORMS`` in
  ``homeassistant/components/mqtt/const.py``;
* for every one of those domains, the members of its ``*DeviceClass`` StrEnum,
  from ``homeassistant/components/<domain>/const.py`` or ``__init__.py``.

Each name is then stamped with the oldest scanned release that has it (and,
if it was dropped again, with the release that lost it).

The scan starts at 2025.1 on purpose.  Device-based MQTT discovery, the only
format the manager publishes, needs Home Assistant 2024.11 or newer, so a main
Home Assistant older than that receives nothing at all and needs no table.

Usage (needs network access; one wheel per minor release, ~40 MB each,
downloaded one at a time and deleted after it is read):

    python3 tools/gen_ha_compat.py
    python3 tools/gen_ha_compat.py --since 2025.1 --out custom_components/integration_manager/ha_compat.json

``--python`` runs pip through another interpreter, which is how this was run
for the committed table: the container has the network, the checkout does not.

    docker cp tools/gen_ha_compat.py <container>:/tmp/gen_ha_compat.py
    docker exec <container> /config/venv-current/bin/python /tmp/gen_ha_compat.py --out /tmp/ha_compat.json
"""

from __future__ import annotations

import argparse
import ast
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import zipfile

PACKAGE = "homeassistant"
MQTT_CONST = "homeassistant/components/mqtt/const.py"
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "custom_components", "integration_manager", "ha_compat.json")


def minor(version: str) -> str:
    """"2026.8.3" -> "2026.8": the table is keyed by release, not by patch."""
    year, month = version.split(".")[:2]
    return f"{int(year)}.{int(month)}"


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".")[:3])


def available_versions(python: str) -> list[str]:
    """Every published version of Home Assistant, newest first."""
    out = subprocess.run([python, "-m", "pip", "index", "versions", PACKAGE],
                         check=True, capture_output=True, text=True).stdout
    match = re.search(r"Available versions:\s*(.+)", out)
    if not match:
        raise SystemExit("pip index versions returned no version list")
    return [v.strip() for v in match.group(1).split(",") if v.strip()]


def releases_to_scan(python: str, since: str) -> list[str]:
    """The newest patch of every minor release from `since` on, oldest first."""
    floor = _key(since)
    newest: dict[str, str] = {}
    for version in available_versions(python):
        if not re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", version) or _key(version)[:2] < floor[:2]:
            continue
        current = newest.get(minor(version))
        if current is None or _key(version) > _key(current):
            newest[minor(version)] = version
    return sorted(newest.values(), key=_key)


def download(python: str, version: str, work: str) -> str:
    """The wheel of one release, downloaded into `work`; its path."""
    existing = glob.glob(os.path.join(work, f"{PACKAGE}-{version}-*.whl"))
    if existing:
        return existing[0]
    subprocess.run([python, "-m", "pip", "download", "--no-deps", "--only-binary=:all:", "-q",
                    "-d", work, f"{PACKAGE}=={version}"], check=True)
    found = glob.glob(os.path.join(work, f"{PACKAGE}-{version}-*.whl"))
    if not found:
        raise SystemExit(f"no wheel downloaded for {version}")
    return found[0]


def mqtt_platforms(wheel: zipfile.ZipFile) -> set[str]:
    """The domains MQTT has an entity platform for, from ENTITY_PLATFORMS."""
    source = wheel.read(MQTT_CONST).decode("utf-8")
    for node in ast.walk(ast.parse(source)):
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if not isinstance(target, ast.Name) or target.id != "ENTITY_PLATFORMS":
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
            continue
        return {item.attr.lower() for item in node.value.elts
                if isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name) and item.value.id == "Platform"}
    raise SystemExit(f"ENTITY_PLATFORMS not found in {MQTT_CONST}")


def device_classes(wheel: zipfile.ZipFile, domain: str) -> set[str]:
    """The values of the domain's ``*DeviceClass`` StrEnum, if it has one."""
    names = set(wheel.namelist())
    found: set[str] = set()
    for candidate in (f"homeassistant/components/{domain}/const.py",
                      f"homeassistant/components/{domain}/__init__.py"):
        if candidate not in names:
            continue
        for node in ast.walk(ast.parse(wheel.read(candidate).decode("utf-8"))):
            if not isinstance(node, ast.ClassDef) or not node.name.endswith("DeviceClass"):
                continue
            if not any(isinstance(base, ast.Name) and base.id == "StrEnum" for base in node.bases):
                continue
            for member in node.body:
                if isinstance(member, ast.Assign) and isinstance(member.value, ast.Constant) \
                        and isinstance(member.value.value, str):
                    found.add(member.value.value)
    return found


def scan(wheel_path: str) -> tuple[set[str], dict[str, set[str]]]:
    """(MQTT platforms, {domain: device classes}) of one release."""
    with zipfile.ZipFile(wheel_path) as wheel:
        platforms = mqtt_platforms(wheel)
        classes = {domain: found for domain in sorted(platforms) if (found := device_classes(wheel, domain))}
    return platforms, classes


def build(python: str, since: str, work: str, keep: bool) -> dict:
    """Download and read every release from `since` on; the table."""
    versions = releases_to_scan(python, since)
    if not versions:
        raise SystemExit(f"no Home Assistant release found from {since} on")
    os.makedirs(work, exist_ok=True)
    platform_first: dict[str, str] = {}
    platform_gone: dict[str, str] = {}
    class_first: dict[str, dict[str, str]] = {}
    class_gone: dict[str, dict[str, str]] = {}
    for version in versions:
        wheel_path = download(python, version, work)
        try:
            platforms, classes = scan(wheel_path)
        finally:
            if not keep:
                os.unlink(wheel_path)
        release = minor(version)
        print(f"  {version}: {len(platforms)} MQTT platforms, "
              f"{sum(len(c) for c in classes.values())} device classes in {len(classes)} domains", flush=True)
        for name in platforms:
            platform_first.setdefault(name, release)
            platform_gone.pop(name, None)  # back again: the window reopens
        for name in platform_first:
            if name not in platforms:
                platform_gone.setdefault(name, release)
        for domain, found in classes.items():
            first, gone = class_first.setdefault(domain, {}), class_gone.setdefault(domain, {})
            for name in found:
                first.setdefault(name, release)
                gone.pop(name, None)
            for name in first:
                if name not in found:
                    gone.setdefault(name, release)
    return {
        "generated": {
            "at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "generator": "tools/gen_ha_compat.py",
            "oldest": minor(versions[0]),
            "newest": minor(versions[-1]),
            "versions": versions,
        },
        # name -> the oldest scanned release that has it.  "oldest" above means
        # "at or before that release": nothing older was looked at.
        "platforms": dict(sorted(platform_first.items())),
        "device_classes": {domain: dict(sorted(names.items())) for domain, names in sorted(class_first.items())},
        # name -> the release that dropped it again, for the rare removal
        "removed": {
            "platforms": dict(sorted(platform_gone.items())),
            "device_classes": {domain: dict(sorted(names.items()))
                               for domain, names in sorted(class_gone.items()) if names},
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default="2025.1", help="oldest release to scan (default: 2025.1)")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"where to write the table (default: {DEFAULT_OUT})")
    parser.add_argument("--work", default="/tmp/compat", help="download directory (default: /tmp/compat)")
    parser.add_argument("--python", default=sys.executable, help="interpreter whose pip downloads the wheels")
    parser.add_argument("--keep", action="store_true", help="keep the wheels instead of deleting each after it is read")
    args = parser.parse_args(argv)

    print(f"scanning Home Assistant from {args.since} with {args.python}", flush=True)
    table = build(args.python, args.since, args.work, args.keep)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(table, handle, indent=1, sort_keys=False)
        handle.write("\n")
    generated = table["generated"]
    classes = sum(len(names) for names in table["device_classes"].values())
    removed = len(table["removed"]["platforms"]) + sum(len(n) for n in table["removed"]["device_classes"].values())
    print(f"wrote {args.out}: {len(generated['versions'])} releases scanned "
          f"({generated['oldest']} .. {generated['newest']}), "
          f"{len(table['platforms'])} MQTT platforms, "
          f"{classes} device classes in {len(table['device_classes'])} domains, "
          f"{removed} removed, {os.path.getsize(args.out)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
