"""The app, read by the Supervisor's own code instead of a copy of its schema.

    python .github/app_supervisor_check.py <supervisor checkout> [<repository root>]

The linter CI runs (frenck/action-app-linter) checks app/config.yaml against a JSON schema kept by hand; it does not
follow the Supervisor.  This imports the Supervisor from a checkout of home-assistant/supervisor (its requirements
installed, the package itself never: "supervisor" on PyPI is supervisord) and runs what the Supervisor runs when it
reads this repository:

  - discovery: the config.* files its store finds (supervisor/store/data.py _find_app_configs) are exactly app/;
  - SCHEMA_APP_CONFIG on app/config.yaml.  Any warning it logs fails, except the lines of app_supervisor_allow.txt;
    the schema drops unknown keys without a word (REMOVE_EXTRA), so a key of ours it no longer knows fails too;
  - SCHEMA_APP_TRANSLATIONS on app/translations/*, the same way;
  - AppOptions on the default options, the check an app's start runs;
  - App._is_excluded_by_filter, the backup's filter, over the app's folder named as the Supervisor names it: what
    backupkit.APP_BACKUP_EXCLUDE_GLOBS names (what HRI's own backups leave out, but those backups) must stay out of
    the Supervisor's (the venv alone is about 800 MB), and the live state (KEEP_LIVE_GLOBS, settings, installed
    versions, patches) and HRI's own backups (a restore would delete them) must stay in.  The
    Supervisor matches the patterns against the full path today and says it may switch to the relative one
    (apps/app.py, _is_excluded_by_filter's docstring): this is what notices.

Every section runs and prints its problems; the exit status is 1 when any section failed.
"""

import copy
import logging
import pathlib
import re
import sys
from pathlib import PurePath
from types import SimpleNamespace

HERE = pathlib.Path(__file__).resolve().parent
ALLOW = HERE / "app_supervisor_allow.txt"
# what a real install calls the app's folder: <repository hash>_<slug> below the Supervisor's app_configs
CONFIG_PARENT = PurePath("/data/app_configs")
REPO_HASH = "5c53de3b"
# the state an app's backup must keep, next to KEEP_LIVE_GLOBS (relative to the app's folder, the /config volume)
STATE_FILES = (
    "configuration.yaml", ".storage/core.config_entries", ".storage/core.device_registry",
    "integration_manager/settings.json", "integration_manager/state.json", "integration_manager/mqtt.json",
    "integration_manager/versions/ramses_cc/0.55.1/custom_components/ramses_cc/__init__.py",
    "integration_manager/patches/ramses_cc/fix.patch", "custom_components/ramses_cc/manifest.json",
    # HRI's own backups, the one a Full rollback needs among them
    "backups/20260926-120000-pre-update.zip", "integration_manager/backups/x.zip",
)


class Warnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def allowed_lines(path: pathlib.Path = ALLOW) -> list[str]:
    if not path.is_file():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def unexpected(messages: list[str], allow: list[str]) -> list[str]:
    return [m for m in messages if not any(a in m for a in allow)]


def dropped_keys(given, kept, path: str = "") -> list[str]:
    """Keys of ``given`` the validated ``kept`` no longer has, at any depth where both are mappings (or lists of the
    same length)."""
    out = []
    if isinstance(given, dict) and isinstance(kept, dict):
        for key, value in given.items():
            where = f"{path}.{key}" if path else str(key)
            if key not in kept:
                out.append(where)
            else:
                out += dropped_keys(value, kept[key], where)
    elif isinstance(given, list) and isinstance(kept, list) and len(given) == len(kept):
        for i, (a, b) in enumerate(zip(given, kept)):
            out += dropped_keys(a, b, f"{path}[{i}]")
    return out


def example_path(glob: str, star: str) -> str:
    """A file name ``glob`` matches: character classes become their first character, * becomes ``star``."""
    return re.sub(r"\[(.)[^\]]*\]", r"\1", glob).replace("*", star)


def discovered(root: pathlib.Path, suffixes) -> list[str]:
    # supervisor/store/data.py _find_app_configs: every config.* below the repository, skipping path parts that
    # start with "." or are "rootfs"
    return sorted(
        p.relative_to(root).as_posix() for p in root.glob("**/config.*")
        if not [part for part in p.parts if part.startswith(".") or part == "rootfs"] and p.suffix in suffixes
    )


class Check:
    def __init__(self, supervisor: pathlib.Path, root: pathlib.Path):
        sys.path.insert(0, str(supervisor))
        sys.path.insert(0, str(root))
        self.root = root
        self.failed = False
        self.allow = allowed_lines()
        self.handler = Warnings()
        logging.getLogger().addHandler(self.handler)
        logging.getLogger().setLevel(logging.WARNING)

    def section(self, title: str, problems: list[str]) -> None:
        if problems:
            self.failed = True
            print(f"FAIL  {title}")
            for p in problems:
                print(f"      - {p}")
        else:
            print(f"ok    {title}")

    def warnings_since(self, start: int) -> list[str]:
        seen = self.handler.records[start:]
        for m in seen:
            if not unexpected([m], self.allow):
                print(f"      (allowed) {m}")
        return [f"warning: {m}" for m in unexpected(seen, self.allow)]

    def run(self) -> int:
        import voluptuous as vol
        from supervisor.apps.app import App
        from supervisor.apps.options import AppOptions
        from supervisor.apps.validate import SCHEMA_APP_CONFIG, SCHEMA_APP_TRANSLATIONS
        from supervisor.const import FILE_SUFFIX_CONFIGURATION
        from supervisor.utils.common import read_json_or_yaml_file

        import backupkit

        found = discovered(self.root, FILE_SUFFIX_CONFIGURATION)
        self.section("the Supervisor finds one app, app/", [] if found == ["app/config.yaml"] else [f"found {found}"])

        start = len(self.handler.records)
        raw = read_json_or_yaml_file(self.root / "app" / "config.yaml")
        problems = []
        try:
            config = SCHEMA_APP_CONFIG(copy.deepcopy(raw))  # its migrations change what they are given
        except vol.Invalid as err:
            config = None
            problems.append(f"invalid: {err}")
        problems += self.warnings_since(start)
        if config is not None:
            problems += [f"dropped by the schema (unknown to this Supervisor): {k}" for k in dropped_keys(raw, config)]
        self.section("app/config.yaml against SCHEMA_APP_CONFIG", problems)
        if config is None:
            return 1

        for path in sorted((self.root / "app" / "translations").glob("*")):
            if path.suffix not in FILE_SUFFIX_CONFIGURATION:
                continue
            start = len(self.handler.records)
            raw = read_json_or_yaml_file(path)
            problems = []
            try:
                problems += [f"dropped by the schema: {k}" for k in dropped_keys(raw, SCHEMA_APP_TRANSLATIONS(copy.deepcopy(raw)))]
            except vol.Invalid as err:
                problems.append(f"invalid: {err}")
            unknown = set((raw.get("configuration") or {})) - set(config.get("schema") or {})
            problems += [f"translates an option the app does not have: {k}" for k in sorted(unknown)]
            problems += self.warnings_since(start)
            self.section(f"app/translations/{path.name} against SCHEMA_APP_TRANSLATIONS", problems)

        start = len(self.handler.records)
        problems = []
        try:
            AppOptions(None, config["schema"], config["name"], config["slug"])(config["options"])
        except vol.Invalid as err:
            problems.append(f"invalid: {err}")
        problems += self.warnings_since(start)
        self.section("the default options against the app's option schema (AppOptions)", problems)

        folder = CONFIG_PARENT / f"{REPO_HASH}_{config['slug']}"
        app = SimpleNamespace(backup_exclude=config.get("backup_exclude") or [])

        def excluded(rel: str) -> bool:
            # the backup's walk (securetar atomic_contents_add) never descends into a folder the filter refuses, so
            # a file is out when it or any folder above it is
            parts = PurePath(rel).parts
            return any(
                App._is_excluded_by_filter(app, folder, "config", PurePath("config", *parts[:i]))
                for i in range(1, len(parts) + 1)
            )

        out = sorted({example_path(g, "x1") for g in backupkit.APP_BACKUP_EXCLUDE_GLOBS})
        keep = sorted({example_path(g, "") for g in backupkit.KEEP_LIVE_GLOBS} | set(STATE_FILES))
        problems = [f"kept, but the app's backup must leave it out: {rel}" for rel in out if not excluded(rel)]
        problems += [f"left out, but it is live state: {rel}" for rel in keep if excluded(rel)]
        self.section(f"backup_exclude under {folder} ({len(out)} left out, {len(keep)} kept)", problems)
        return 1 if self.failed else 0


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.split("\n\n", 2)[1], file=sys.stderr)
        return 2
    supervisor = pathlib.Path(argv[1]).resolve()
    root = pathlib.Path(argv[2]).resolve() if len(argv) == 3 else HERE.parent
    if not (supervisor / "supervisor" / "apps" / "validate.py").is_file():
        print(f"{supervisor} is not a checkout of home-assistant/supervisor with supervisor/apps/", file=sys.stderr)
        return 2
    return Check(supervisor, root).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
