FROM python:3.14-slim

# Home Assistant is NOT baked in: entrypoint.py installs the wanted version
# into /config/venv-<version> (on the volume) at start, so the manager UI
# can update or roll back HA with a restart, and image rebuilds are cheap.
ARG HA_VERSION=2026.8.3
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HRI_CONFIG=/config \
    HRI_PORT=8087 \
    HA_VERSION_DEFAULT=${HA_VERSION} \
    MALLOC_ARENA_MAX=2 \
    TZ=UTC

WORKDIR /app

COPY entrypoint.py run.py logbuffer.py backupkit.py jsonio.py registry.json requirements.txt /app/
# NOT under a directory named custom_components: HA's loader imports the
# namespace package `custom_components` once, and a second such directory
# reachable from sys.path (cwd) shadows /config/custom_components.
COPY custom_components/integration_manager /app/manager_src/integration_manager
COPY patches /app/patches

VOLUME ["/config"]
EXPOSE 8087

CMD ["python", "/app/entrypoint.py"]
