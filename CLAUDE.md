# CLAUDE.md — hass-remote-integration (HRI)

Headless Home Assistant container that runs one custom integration and mirrors it to a parent HA over MQTT discovery, managed by the web UI/API in `custom_components/integration_manager/`.

Operator-specific values (container names, hosts, ports, local paths) live in `CLAUDE.local.md`, which is gitignored. Never commit them here.

## Hard rules (apply to every agent and subagent)
- Never restart, stop, recreate or `docker exec` into any production container (the operator lists them in `CLAUDE.local.md`), and never call the operator's production Home Assistant port.
- Unit tests may run inside the operator's designated test container only; never restart or recreate it without explicit permission.
- Throwaway resources are named `hri-<name>-*` (container, broker, network, volume), listen on 127.0.0.1 only, and are removed at the end of the task even on failure.
- Never print passwords, tokens, cookies or keys. Secrets live in the data volume, never in the repo. `.env` and `docker-compose.override.yml` are local and gitignored.
- Subagents do not edit the main checkout: work in a git worktree next to it (`git worktree add ../<repo>-wt-<topic> -b fix/<topic> origin/main`) and remove it when done.

## Tests
Unit tests run inside a Home Assistant container with the HA venv, in a private directory per task (`TEST_CONTAINER` and `HA_PYTHON` come from `CLAUDE.local.md`):
```bash
D=/tmp/hri-tests-<topic>; docker exec "$TEST_CONTAINER" sh -c "rm -rf $D && mkdir -p $D/custom_components"
for f in tests jsonio.py backupkit.py logbuffer.py entrypoint.py; do docker cp -q "$f" "$TEST_CONTAINER:$D/"; done && docker cp -q custom_components/integration_manager "$TEST_CONTAINER:$D/custom_components/"
docker exec -w $D -e PYTHONPATH=$D -e PYTHONDONTWRITEBYTECODE=1 "$TEST_CONTAINER" "$HA_PYTHON" -m unittest discover -s tests -t .
```
JS tests run on the host (CI does the same): `python -m unittest discover -s tests -t . -p "test_*_js.py"`.
Check the exit code, never `| tail` the runner. All tests must pass before a commit.

## End-to-end harness
`tests/e2e/stack.sh` (usage in its header) brings up broker + throwaway parent HA + HRI with `hri_probe` (repo `trailro/hri-test-integration`). Use the `hri-e2e` subagent for campaigns.

## Release checklist
Version bump → CI green → container image verified → `docs-drift-check` subagent (docs vs code since previous tag) → shadow redeploy.
