"""The app on a real Home Assistant OS: install it from this repository, back it up, restore it.

Runs under Home Assistant OS's own test harness (labgrid over the VM's serial console, the operating-system
repository's tests/conftest.py and qemu_shell_strategy.py), as its tests/supervisor_test/test_supervisor.py does, with
.github/haos/strategy.yaml.  REPO_URL names this repository and the ref to install from (<url>#<ref>).  The image the
Supervisor pulls is ghcr.io/trailro/hass-remote-integration:<the version in app/config.yaml>, the last release: what
this checks is the app as the Supervisor sees it (the repository, config.yaml, the backup filter), not an unreleased
image.  The app's port 8087 is forwarded to the runner, so /api/status is asked from outside, the way a user's
browser reaches it.
"""

import hashlib
import json
import logging
import os
import time
import urllib.request

import pytest
from labgrid.driver import ExecutionError

logger = logging.getLogger(__name__)
REPO_URL = os.environ["REPO_URL"]
STATUS = f"http://127.0.0.1:{os.environ.get('HRI_PORT', '8087')}/api/status"
SLUG = "hass_remote_integration"
MAX_BACKUP = 20 * 1024 * 1024


@pytest.fixture(scope="module")
def stash() -> dict:
    return {}


def _wait(what: str, check, timeout: float, every: float = 5):
    start = time.monotonic()
    while True:
        try:
            got = check()
        except (ExecutionError, ValueError) as err:  # a restarting Supervisor or CLI, or an answer that is not JSON yet
            got = None
            logger.info("%s: %s", what, err)
        if got:
            logger.info("%s after %.0f s", what, time.monotonic() - start)
            return got
        if time.monotonic() - start > timeout:
            raise AssertionError(f"{what}: not within {timeout:.0f} s")
        time.sleep(every)


def _status():
    try:
        with urllib.request.urlopen(STATUS, timeout=5) as resp:
            body = json.load(resp)
    except OSError:
        return None
    return body if "ha_version" in body else None


@pytest.mark.dependency()
@pytest.mark.timeout(1500)
def test_supervisor_and_core(shell, shell_json):
    # as test_supervisor.py: no auto update in the middle of the test; the Supervisor is moved to the newest stable
    # below, once, on purpose
    shell.run_check(
        "jq '.auto_update = false' /mnt/data/supervisor/updater.json > /tmp/updater.json"
        " && mv /tmp/updater.json /mnt/data/supervisor/updater.json"
        " && systemctl restart haos-supervisor.service"
    )
    _wait("Supervisor answers", lambda: shell_json("ha supervisor info --no-progress --raw-json").get("result") == "ok", 600)
    info = shell_json("ha supervisor info --no-progress --raw-json")["data"]
    logger.info("Supervisor %s, newest %s", info.get("version"), info.get("version_latest"))
    if info.get("version") != info.get("version_latest"):
        try:
            shell_json("ha supervisor update --no-progress --raw-json", timeout=600)
        except (ExecutionError, ValueError) as err:  # the Supervisor restarts under the call
            logger.info("supervisor update: %s", err)

        def updated():
            data = shell_json("ha supervisor info --no-progress --raw-json").get("data") or {}
            return data.get("version") and data.get("version") == data.get("version_latest")
        _wait("Supervisor updated", updated, 600)

    def core_up():
        core = shell_json("ha core info --no-progress --raw-json").get("data") or {}
        jobs = shell_json("ha jobs info --no-progress --raw-json").get("data", {}).get("jobs", [])
        installing = any(j.get("name") == "home_assistant_core_install" and not j.get("done") for j in jobs)
        return core.get("version") not in (None, "", "landingpage") and not installing and core
    core = _wait("Home Assistant Core installed", core_up, 1200, every=10)
    logger.info("Core %s", core.get("version"))
    os_info = shell_json("ha os info --no-progress --raw-json")["data"]
    logger.info("Home Assistant OS %s", os_info.get("version"))


@pytest.mark.dependency(depends=["test_supervisor_and_core"])
@pytest.mark.timeout(1500)
def test_install_and_start(shell, shell_json, stash):
    result = shell_json(f"ha store add '{REPO_URL}' --no-progress --raw-json", timeout=300)
    assert result.get("result") == "ok", f"adding {REPO_URL}: {result}"
    # supervisor/store/utils.py get_hash_from_repository: the repository's slug is its URL's hash
    slug = f"{hashlib.sha1(REPO_URL.lower().encode()).hexdigest()[:8]}_{SLUG}"
    info = shell_json(f"ha apps info {slug} --no-progress --raw-json")
    assert info.get("result") == "ok", f"the store does not offer {slug}: {info}"
    stash["slug"] = slug
    logger.info("store offers %s version %s", slug, info["data"].get("version_latest"))

    result = shell_json(f"ha apps install {slug} --no-progress --raw-json", timeout=1200)
    assert result.get("result") == "ok", f"install: {result}"
    result = shell_json(f"ha apps start {slug} --no-progress --raw-json", timeout=300)
    assert result.get("result") == "ok", f"start: {result}"
    status = _wait("the app's /api/status (a fresh app installs Home Assistant first)", _status, 1200, every=10)
    logger.info("app answers: Home Assistant %s", status.get("ha_version"))
    venvs = shell.run_check(f"ls -d /mnt/data/supervisor/app_configs/{slug}/venv-* 2>/dev/null || "
                            f"ls -d /mnt/data/supervisor/addon_configs/{slug}/venv-*")
    assert venvs, "no venv in the app's folder: the backup check below would prove nothing"
    logger.info("venvs: %s", venvs)


@pytest.mark.dependency(depends=["test_install_and_start"])
@pytest.mark.timeout(900)
def test_backup_leaves_out_the_venv(shell, shell_json, stash):
    slug = stash["slug"]
    result = shell_json(f"ha backups new --app {slug} --name app-canary --no-progress --raw-json", timeout=600)
    assert result.get("result") == "ok", f"backup: {result}"
    backup = result["data"]["slug"]
    stash["backup"] = backup
    path = f"/mnt/data/supervisor/backup/{backup}.tar"
    size = int("".join(shell.run_check(f"stat -c %s {path}")))
    logger.info("backup %s: %d bytes", backup, size)
    assert size < MAX_BACKUP, f"the app's backup is {size} bytes: backup_exclude no longer leaves out the venv"
    # counted in the VM: the list itself is long for a serial console
    listing = f"tar -xOf {path} ./{slug}.tar.gz | tar -tzf -"
    count = lambda pattern: int("".join(shell.run_check(f"{listing} | grep -cE '{pattern}' || true", timeout=300)))  # noqa: E731
    total, venv, state = count("."), count("(^|/)venv-"), count("(^|/)integration_manager/")
    logger.info("the app's archive: %d members, %d of a venv, %d of integration_manager/", total, venv, state)
    assert venv == 0, "the venv is in the backup"
    assert state > 0, "the app's state is not in the backup"


@pytest.mark.dependency(depends=["test_backup_leaves_out_the_venv"])
@pytest.mark.timeout(1500)
def test_restore_brings_it_back(shell_json, stash):
    slug, backup = stash["slug"], stash["backup"]
    result = shell_json(f"ha backups restore {backup} --app {slug} --homeassistant=false --no-progress --raw-json",
                        timeout=900)
    assert result.get("result") == "ok", f"restore: {result}"
    _wait("the app started after the restore",
          lambda: shell_json(f"ha apps info {slug} --no-progress --raw-json").get("data", {}).get("state") == "started", 600)
    # the venv stayed out of the backup, so the restored app installs Home Assistant again before it answers
    status = _wait("the restored app's /api/status", _status, 1200, every=10)
    logger.info("restored app answers: Home Assistant %s", status.get("ha_version"))
