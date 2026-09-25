"""The Home Assistant app (app/config.yaml, repository.yaml): the options it takes and how entrypoint.py turns them into
the environment a plain Docker install sets, what the Supervisor's backup of it leaves out, and that it is the only
app the Supervisor finds in this repository."""

import fnmatch
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import backupkit
from tests.fakes import entrypoint_for

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_CONFIG = ROOT / "app" / "config.yaml"
# how the Supervisor finds apps: supervisor/store/data.py _find_app_configs (every config.* below the repository,
# skipping path parts that start with "." and "rootfs") and const.py FILE_SUFFIX_CONFIGURATION
APP_SUFFIXES = (".json", ".yaml", ".yml")
# supervisor/apps/options.py RE_SCHEMA_ELEMENT (the scalar forms)
RE_SCHEMA_ELEMENT = re.compile(
    r"^(?:|bool|email|url|port|device(?:\((?P<filter>subsystem=[a-z]+)\))?|str(?:\((?P<s_min>\d+)?,(?P<s_max>\d+)?\))?"
    r"|password(?:\((?P<p_min>\d+)?,(?P<p_max>\d+)?\))?|int(?:\((?P<i_min>-?\d+)?,(?P<i_max>-?\d+)?\))?"
    r"|float(?:\((?P<f_min>-?\d*\.?\d+)?,(?P<f_max>-?\d*\.?\d+)?\))?|match\((?P<match>.*)\)|list\((?P<list>.+)\))\??$"
)
VARS = ("HRI_PASSWORD", "HRI_APT_PACKAGES", "HRI_CALL_TIMEOUT", "HA_VERSION_LATEST", "HRI_DEBUG", "HRI_COOKIE_SECURE")


def _yaml(path):
    import yaml  # the HA venv has it; the CI job that runs only the discovery test does not need it
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def supervisor_backup_exclude(globs, slug):
    """backup_exclude for ``globs`` (relative to the config folder, fnmatch as backupkit._excluded uses them).  The
    Supervisor tests every entry with PurePath.match against the FULL path of each file and folder, and leaves out a
    matching folder with everything below it: a pattern matches the end of that path.  A pattern starting with "*"
    matches at any depth for backupkit too and stays as it is; any other one is tied to the top of the app's folder,
    /…/<repository hash or "local">_<slug>, by "*_<slug>/".  "x/*" is dropped where "x" is there: nothing below a
    folder left out is looked at."""
    return [g if g.startswith("*") else f"*_{slug}/{g}" for g in globs if not (g.endswith("/*") and g[:-2] in globs)]


def supervisor_archive(root: pathlib.Path, backup_exclude) -> set[str]:
    """The files the Supervisor puts in the "config" part of an app backup, relative to ``root``: its walk
    (securetar atomic_contents_add, which skips a folder the filter refuses without descending) with its filter
    (supervisor/apps/app.py App._is_excluded_by_filter: full_path.match(exclude) for each entry)."""
    kept = set()

    def excluded(path: pathlib.PurePath) -> bool:
        return any(path.match(g) for g in backup_exclude)

    def walk(folder: pathlib.Path):
        for item in folder.iterdir():
            if excluded(item):
                continue
            if item.is_dir() and not item.is_symlink():
                walk(item)
            else:
                kept.add(item.relative_to(root).as_posix())

    walk(root)
    return kept


class AppConfigTest(unittest.TestCase):
    def setUp(self):
        if not APP_CONFIG.is_file():
            self.skipTest("app/ not copied next to the tests")
        self.cfg = _yaml(APP_CONFIG)

    def test_repository_yaml(self):
        repo = _yaml(ROOT / "repository.yaml")
        self.assertIsInstance(repo.get("name"), str)
        self.assertEqual(repo.get("url"), "https://github.com/trailro/hass-remote-integration")
        self.assertIsInstance(repo.get("maintainer"), str)

    def test_every_option_has_one_variable_and_back(self):
        ep = entrypoint_for(self, tempfile.mkdtemp())
        self.assertEqual(set(self.cfg["schema"]), set(ep.APP_OPTIONS))
        self.assertLessEqual(set(self.cfg["options"]), set(self.cfg["schema"]))
        variables = [var for var, _ in ep.APP_OPTIONS.values()]
        self.assertEqual(len(variables), len(set(variables)))
        for name, kind in self.cfg["schema"].items():
            self.assertRegex(kind, RE_SCHEMA_ELEMENT, name)
            is_bool = ep.APP_OPTIONS[name][1] is not None
            self.assertEqual(kind == "bool", is_bool, name)
            if is_bool:
                self.assertIn(name, self.cfg["options"], f"{name}: a bool with no default shows as unset")
        self.assertTrue(self.cfg["schema"]["password"].startswith("password"))
        self.assertTrue(self.cfg["schema"]["password"].endswith("?"))

    def test_the_fields_hri_needs(self):
        with open(ROOT / "docker-compose.yml", encoding="utf-8") as fh:
            grace = re.search(r"stop_grace_period:\s*(\d+)s", fh.read()).group(1)
        self.assertEqual(self.cfg["timeout"], int(grace))
        self.assertLessEqual(self.cfg["timeout"], 300)  # the Supervisor's limit
        self.assertEqual(self.cfg["map"], [{"type": "addon_config", "read_only": False}])
        self.assertEqual(self.cfg["ports"], {"8087/tcp": 8087})
        self.assertEqual(self.cfg["webui"], "http://[HOST]:[PORT:8087]")
        self.assertNotIn("ingress", self.cfg)  # the UI does not work under a path prefix
        self.assertNotIn("init", self.cfg)  # Docker's init, the default, as the compose file's init: true
        self.assertNotIn("environment", self.cfg)  # options go through /data/options.json
        self.assertEqual(sorted(self.cfg["arch"]), ["aarch64", "amd64"])
        self.assertEqual(self.cfg["image"], "ghcr.io/trailro/hass-remote-integration")
        self.assertRegex(self.cfg["version"], r"^\d+\.\d+\.\d+$")
        with open(ROOT / "custom_components" / "integration_manager" / "manifest.json", encoding="utf-8") as fh:
            manifest = json.load(fh)["version"]
        key = lambda v: tuple(int(x) for x in v.split("."))  # noqa: E731
        # a release PR moves the manifest; the app follows once the image is pushed (image.yml), never before
        self.assertLessEqual(key(self.cfg["version"]), key(manifest))

    def test_backup_exclude_is_derived_from_backupkit(self):
        self.assertEqual(self.cfg["backup_exclude"], supervisor_backup_exclude(backupkit.DISPOSABLE_GLOBS, self.cfg["slug"]))
        self.assertIn(f"*_{self.cfg['slug']}/venv-*", self.cfg["backup_exclude"])
        # a Supervisor restore replaces the whole folder: what backupkit keeps out only so that a restore leaves the
        # live copy alone would be deleted by it
        self.assertEqual(sorted(backupkit.EXCLUDE_GLOBS), sorted(backupkit.DISPOSABLE_GLOBS + backupkit.KEEP_LIVE_GLOBS))

    def test_backup_exclude_leaves_out_what_backupkit_leaves_out(self):
        """The Supervisor's matching, run on a folder named as the Supervisor names it, drops exactly the files
        backupkit's DISPOSABLE_GLOBS drop, and nothing else - not a same-named folder deeper down."""
        slug = self.cfg["slug"]
        root = pathlib.Path(tempfile.mkdtemp()) / f"0123abcd_{slug}"
        files = [
            "configuration.yaml", ".storage/core.config_entries", ".storage/core.uuid", ".storage/http",
            ".storage/tmpab12cd_9", ".storage/tmpab12cd", ".storage/core.restore_state.pre-import", ".storage/x.log",
            "venv-2026.9.3/bin/python", "venv-2026.9.3/lib/site.py", "backups/hri-1.zip", "deps/lib/a.py", "tts/a.mp3",
            "home-assistant.log", "home-assistant.log.1", "__pycache__/a.pyc", "blueprints/a.yaml", "www/a.png",
            "custom_components/foo/__init__.py", "custom_components/foo/__pycache__/x.cpython-314.pyc",
            "custom_components/foo/backups/keep.py", "custom_components/foo/venv-x/keep.py", "custom_components/foo/deps/k.py",
            "integration_manager/state.json", "integration_manager/settings.json", "integration_manager/auth_key",
            "integration_manager/auth_revoked", "integration_manager/events.jsonl", "integration_manager/events.jsonl.1",
            "integration_manager/mqtt_identity.json", "integration_manager/ha-install.log", "integration_manager/a.tmp",
            "integration_manager/restore-pending-1.zip", "integration_manager/restore-pending.json",
            "integration_manager/staging-restore-1/deep/f", "integration_manager/import-extracted/.storage/x",
            "integration_manager/import.tar", "integration_manager/backups/x", "integration_manager/pre-restore-x/y",
        ]
        for rel in files:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x")
        (root / "venv-current").symlink_to(root / "venv-2026.9.3")
        want = {rel for rel in files if not any(fnmatch.fnmatch(rel, g) for g in backupkit.DISPOSABLE_GLOBS)}
        self.assertEqual(supervisor_archive(root, self.cfg["backup_exclude"]), want)
        for rel in ("custom_components/foo/backups/keep.py", "custom_components/foo/venv-x/keep.py", ".storage/core.uuid",
                    ".storage/http", "integration_manager/events.jsonl", "integration_manager/mqtt_identity.json"):
            self.assertIn(rel, want)
        for rel in ("venv-2026.9.3/bin/python", "backups/hri-1.zip", "home-assistant.log", "integration_manager/auth_key",
                    ".storage/tmpab12cd_9", "integration_manager/staging-restore-1/deep/f"):
            self.assertNotIn(rel, want)


class AppDiscoveryTest(unittest.TestCase):
    def test_the_only_app_is_app(self):
        """The Supervisor takes every config.json/.yaml/.yml in the repository as an app: a fixture of that name
        anywhere else would be offered in the App Store (or break the repository)."""
        found = sorted(
            p.relative_to(ROOT).as_posix() for p in ROOT.glob("**/config.*")
            if p.suffix in APP_SUFFIXES and not any(part.startswith(".") or part == "rootfs" for part in p.relative_to(ROOT).parts)
        )
        if not (ROOT / "app").is_dir():
            self.skipTest("app/ not copied next to the tests")
        self.assertEqual(found, ["app/config.yaml"])
        self.assertTrue((ROOT / "repository.yaml").is_file())


@unittest.skipUnless(shutil.which("git") and shutil.which("bash") and sys.platform.startswith("linux"),
                     "needs git, bash and the sed of the Linux runner")
class AppVersionStepTest(unittest.TestCase):
    """image.yml's own "Set app/config.yaml version" script (not a copy), run on a scratch repository with a remote."""

    def setUp(self):
        wf_path = ROOT / ".github" / "workflows" / "image.yml"
        if not wf_path.is_file() or not APP_CONFIG.is_file():
            self.skipTest("workflows or app/ not copied next to the tests")
        wf = _yaml(wf_path)
        job = wf["jobs"]["app-version"]
        self.assertEqual(job["needs"], "image")
        self.assertEqual(job["if"], "needs.image.result == 'success'")
        self.assertEqual(job["permissions"], {"contents": "write"})
        self.script = next(s["run"] for s in job["steps"] if s.get("name") == "Set app/config.yaml version")
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.remote, self.work = tmp / "remote.git", tmp / "work"
        env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
        self.env = env
        git = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True)  # noqa: E731
        self.git = git
        git("init", "-q", "--bare", "-b", "main", str(self.remote))
        git("clone", "-q", str(self.remote), str(self.work))
        (self.work / "app").mkdir()
        (self.work / "app" / "config.yaml").write_text(APP_CONFIG.read_text(encoding="utf-8").replace(
            f'version: "{_yaml(APP_CONFIG)["version"]}"', 'version: "0.24.0"'), encoding="utf-8")
        git("add", "app", cwd=self.work)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "b", cwd=self.work)
        for tag in ("v0.24.0", "v0.25.0", "v0.26.0b1"):
            git("tag", tag, cwd=self.work)
        git("push", "-q", "origin", "HEAD:main", "--tags", cwd=self.work)

    def _run(self, tag, prerelease="false"):
        env = {**self.env, "TAG": tag, "PRERELEASE": prerelease, "BRANCH": "main"}
        proc = subprocess.run(["bash", "-e", "-c", self.script], cwd=self.work, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        shown = subprocess.run(["git", "--git-dir", str(self.remote), "show", "main:app/config.yaml"], env=self.env,
                               capture_output=True, text=True, check=True).stdout
        return _yaml_text(shown)["version"], proc.stdout

    def test_newest_stable_moves_the_version_once(self):
        self.assertEqual(self._run("v0.24.0")[0], "0.24.0")  # not the newest: stays
        self.assertEqual(self._run("v0.25.0", prerelease="true")[0], "0.24.0")
        version, _ = self._run("v0.25.0")
        self.assertEqual(version, "0.25.0")
        log = subprocess.run(["git", "--git-dir", str(self.remote), "log", "-1", "--format=%s", "main"], env=self.env,
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(log, "app: version 0.25.0, the image of v0.25.0 is published")
        version, out = self._run("v0.25.0")  # again (a manual re-run): nothing to commit
        self.assertEqual(version, "0.25.0")
        self.assertIn("already names 0.25.0", out)
        rest = _yaml_text((self.work / "app" / "config.yaml").read_text(encoding="utf-8"))
        rest.pop("version")
        original = _yaml(APP_CONFIG)
        original.pop("version")
        self.assertEqual(rest, original)  # only the version line changed


def _yaml_text(text):
    import yaml
    return yaml.safe_load(text)


class AppOptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ep = entrypoint_for(self, self.tmp, **{var: "from-docker" for var in VARS})
        self.options = os.path.join(self.tmp, "options.json")

    def _apply(self, options, token="t0ken"):
        with open(self.options, "w", encoding="utf-8") as fh:
            fh.write(options if isinstance(options, str) else json.dumps(options))
        # kept until the test ends: the assertions read what apply_app_options left in the environment
        if token:
            patch = mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": token})
        else:
            patch = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "SUPERVISOR_TOKEN"}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        return self.ep.apply_app_options(self.options)

    def test_not_an_app_changes_nothing(self):
        self.assertIsNone(self._apply({"password": "pw", "debug": True}, token=None))
        self.assertEqual({var: os.environ[var] for var in VARS}, dict.fromkeys(VARS, "from-docker"))
        with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "t0ken"}):  # a token alone is not an app
            self.assertIsNone(self.ep.apply_app_options(os.path.join(self.tmp, "missing.json")))
        self.assertEqual(os.environ["HRI_PASSWORD"], "from-docker")

    def test_options_become_the_variables(self):
        applied = self._apply({"password": "s3cret", "apt_packages": "ffmpeg jq", "call_timeout": 90,
                               "ha_version_latest": False, "debug": True, "cookie_secure": True, "other": "x"})
        self.assertEqual(sorted(applied), sorted(VARS))
        self.assertEqual({var: os.environ[var] for var in VARS}, {
            "HRI_PASSWORD": "s3cret", "HRI_APT_PACKAGES": "ffmpeg jq", "HRI_CALL_TIMEOUT": "90",
            "HA_VERSION_LATEST": "0", "HRI_DEBUG": "1", "HRI_COOKIE_SECURE": "1"})

    def test_empty_and_false_are_unset(self):
        applied = self._apply({"password": "", "apt_packages": "", "ha_version_latest": True, "debug": False,
                               "cookie_secure": False})
        self.assertEqual(applied, ["HA_VERSION_LATEST"])
        self.assertEqual(os.environ["HA_VERSION_LATEST"], "1")
        for var in ("HRI_PASSWORD", "HRI_APT_PACKAGES", "HRI_CALL_TIMEOUT", "HRI_DEBUG", "HRI_COOKIE_SECURE"):
            self.assertNotIn(var, os.environ, var)  # "0" would turn HRI_DEBUG on: run.py tests only that it is set
        self.assertFalse(self.ep.password_configured())

    def test_a_password_of_spaces_is_kept(self):
        self._apply({"password": "   "})
        self.assertEqual(os.environ["HRI_PASSWORD"], "   ")  # auth.py keeps the UI closed and says why

    def test_unreadable_options_refuse(self):
        for bad in ("{not json", "[1, 2]"):
            with self.assertRaises(ValueError) as ctx:
                self._apply(bad)
            self.assertNotIn("not json", str(ctx.exception))
        self.assertEqual(os.environ["HRI_PASSWORD"], "from-docker")

    def test_main_applies_them_first_and_never_logs_a_value(self):
        with open(self.options, "w", encoding="utf-8") as fh:
            json.dump({"password": "s3cret-pw", "apt_packages": "jq", "debug": False, "ha_version_latest": True}, fh)
        seen, lines = {}, []

        def prepare():
            seen.update({var: os.environ.get(var) for var in VARS}, configured=self.ep.password_configured())
            raise SystemExit(7)

        with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "t0ken"}), \
                mock.patch.object(self.ep, "APP_OPTIONS_FILE", self.options), \
                mock.patch.object(self.ep, "_prepare", prepare), \
                mock.patch.object(self.ep, "start_status_server", lambda: None), \
                mock.patch.object(self.ep, "log", lines.append), \
                mock.patch.object(self.ep, "restrict_umask", lambda: 0):
            with self.assertRaises(SystemExit) as ctx:
                self.ep.main()
        self.assertEqual(ctx.exception.code, 7)
        self.assertEqual(seen, {"HRI_PASSWORD": "s3cret-pw", "HRI_APT_PACKAGES": "jq", "HRI_CALL_TIMEOUT": None,
                                "HA_VERSION_LATEST": "1", "HRI_DEBUG": None, "HRI_COOKIE_SECURE": None, "configured": True})
        self.assertTrue(any("Home Assistant app" in line and "HRI_PASSWORD" in line for line in lines), lines)
        self.assertFalse([line for line in lines if "s3cret-pw" in line])

    def test_main_stops_on_unreadable_options(self):
        with open(self.options, "w", encoding="utf-8") as fh:
            fh.write('{"password": "s3cret-pw"')
        lines = []
        with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "t0ken"}), \
                mock.patch.object(self.ep, "APP_OPTIONS_FILE", self.options), \
                mock.patch.object(self.ep, "_prepare", mock.Mock(side_effect=AssertionError("started"))), \
                mock.patch.object(self.ep, "log", lines.append), \
                mock.patch.object(self.ep, "restrict_umask", lambda: 0):
            with self.assertRaises(SystemExit) as ctx:
                self.ep.main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse([line for line in lines if "s3cret-pw" in line])

    def test_the_exec_of_run_py_inherits_them(self):
        """main() os.execv's run.py with no env argument: what apply_app_options set and unset in os.environ is what
        the new program sees (os.environ writes go through putenv/unsetenv)."""
        with open(self.options, "w", encoding="utf-8") as fh:
            json.dump({"password": "s3cret-pw", "debug": False}, fh)
        child = (
            "import json, os, sys; sys.path.insert(0, sys.argv[1]); import entrypoint;"
            "entrypoint.apply_app_options(sys.argv[2]);"
            "os.execv(sys.executable, [sys.executable, '-c',"
            " 'import json, os; print(json.dumps({k: os.environ.get(k) for k in (\"HRI_PASSWORD\", \"HRI_DEBUG\", \"SUPERVISOR_TOKEN\", \"HASSIO_TOKEN\", \"HRI_APP\")}))'])"
        )
        env = {**os.environ, "SUPERVISOR_TOKEN": "t0ken", "HASSIO_TOKEN": "t0ken", "HRI_DEBUG": "from-docker",
               "HRI_CONFIG": self.tmp}
        out = subprocess.run([sys.executable, "-c", child, str(ROOT), self.options], env=env, capture_output=True,
                             text=True, timeout=60, check=True).stdout
        self.assertEqual(json.loads(out), {"HRI_PASSWORD": "s3cret-pw", "HRI_DEBUG": None, "SUPERVISOR_TOKEN": None,
                                           "HASSIO_TOKEN": None, "HRI_APP": "1"})

    def test_the_supervisor_token_does_not_outlive_the_options(self):
        """F1: with the token, anything in the app (an integration) could read or rewrite the app's options through
        http://supervisor, the password among them.  HRI needs it for nothing after reading them."""
        with mock.patch.dict(os.environ, {"HASSIO_TOKEN": "t0ken"}):
            applied = self._apply({"password": "s3cret", "debug": True})
            self.assertNotIn("SUPERVISOR_TOKEN", set(os.environ))  # names only: a failure must not print values
            self.assertNotIn("HASSIO_TOKEN", set(os.environ))
            self.assertEqual(sorted(applied), ["HRI_DEBUG", "HRI_PASSWORD"])
            self.assertEqual((os.environ["HRI_PASSWORD"], os.environ["HRI_DEBUG"]), ("s3cret", "1"))
            self.assertEqual(os.environ[self.ep.APP_MARKER], "1")

    def test_a_second_run_without_the_token_keeps_what_the_first_one_set(self):
        """The gate of apply_app_options is the token: an entrypoint run again in the same environment (no token any
        more) leaves the variables the first run set, the app marker with them."""
        self._apply({"password": "s3cret", "apt_packages": "jq", "debug": False})
        with open(self.options, "w", encoding="utf-8") as fh:
            json.dump({"password": "", "apt_packages": ""}, fh)  # never read: not an app any more
        self.assertIsNone(self.ep.apply_app_options(self.options))
        self.assertEqual((os.environ["HRI_PASSWORD"], os.environ["HRI_APT_PACKAGES"]), ("s3cret", "jq"))
        self.assertNotIn("HRI_DEBUG", os.environ)
        self.assertEqual(os.environ[self.ep.APP_MARKER], "1")
        self.assertTrue(self.ep.password_configured())

    def test_not_an_app_keeps_the_token_and_sets_no_marker(self):
        with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "t0ken", "HASSIO_TOKEN": "t0ken"}):
            os.environ.pop(self.ep.APP_MARKER, None)
            self.assertIsNone(self.ep.apply_app_options(os.path.join(self.tmp, "missing.json")))
            self.assertEqual((os.environ["SUPERVISOR_TOKEN"], os.environ["HASSIO_TOKEN"]), ("t0ken", "t0ken"))
            self.assertNotIn(self.ep.APP_MARKER, os.environ)

    def test_unreadable_options_stop_before_anything_inherits_the_token(self):
        with self.assertRaises(ValueError):
            self._apply("{not json")
        self.assertEqual(os.environ["SUPERVISOR_TOKEN"], "t0ken")  # main() exits 2 here: nothing is exec'd


class AppMqttDefaultHostTest(unittest.TestCase):
    """A fresh app offered "mosquitto" as the broker; the official broker app on Home Assistant OS is
    "core-mosquitto".  Only the default changes, and only in the app: a configured host is never touched."""

    def setUp(self):
        from custom_components.integration_manager import mqtt_publisher as mp

        self.mp = mp
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
        self.pub.path = os.path.join(self.tmp, "mqtt.json")

    def _as_app(self):
        ep = entrypoint_for(self, self.tmp)
        options = os.path.join(self.tmp, "options.json")
        with open(options, "w", encoding="utf-8") as fh:
            json.dump({"password": "pw"}, fh)
        patch = mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "t0ken"})
        patch.start()
        self.addCleanup(patch.stop)
        self.assertIsNotNone(ep.apply_app_options(options))

    def test_plain_docker_keeps_mosquitto(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("HRI_APP", None)
            self.assertEqual(self.mp.MqttConfig().host, "mosquitto")
            self.assertEqual(self.pub._load().host, "mosquitto")

    def test_the_app_defaults_to_core_mosquitto(self):
        self._as_app()
        self.assertEqual(self.mp.MqttConfig().host, "core-mosquitto")
        self.assertEqual(self.pub._load().host, "core-mosquitto")  # no mqtt.json: a fresh app

    def test_a_configured_host_is_never_overridden(self):
        self._as_app()
        for host in ("mosquitto", "192.168.1.5"):
            with self.subTest(host=host):
                with open(self.pub.path, "w", encoding="utf-8") as fh:
                    json.dump({"enabled": True, "host": host}, fh)
                self.assertEqual(self.pub._load().host, host)
        with open(self.pub.path, "w", encoding="utf-8") as fh:
            json.dump({"enabled": True, "port": 1884}, fh)  # saved without a host: none was configured
        self.assertEqual(self.pub._load().host, "core-mosquitto")


if __name__ == "__main__":
    unittest.main()
