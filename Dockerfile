# pinned by digest: a rebuild gets the same base, Dependabot proposes the updates
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

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

# libjpeg-turbo's C library (~0.5 MB): PyTurboJPEG is a ctypes binding and
# finds nothing in the slim base, so HA's camera component - a dependency of
# many integrations - logs an ERROR with a traceback at every boot and cannot
# scale a snapshot.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libturbojpeg0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY entrypoint.py run.py logbuffer.py backupkit.py jsonio.py registry.json requirements.txt /app/
# NOT under a directory named custom_components: HA's loader imports the
# namespace package `custom_components` once, and a second such directory
# reachable from sys.path (cwd) shadows /config/custom_components.
COPY custom_components/integration_manager /app/manager_src/integration_manager
COPY patches /app/patches

# which commit the image was built from, shown in the UI next to the version
# (set by the image and CI workflows; a local build is "local" unless given)
ARG HRI_BUILD=local
ENV HRI_BUILD=${HRI_BUILD}

VOLUME ["/config"]
EXPOSE 8087

# Without this Docker cannot tell whether the manager answers at all.  There is no curl in the image, so
# its own Python asks the manager port - HRI_PORT read at runtime, not the 8087 baked in above - for
# /api/status without X-Requested-With: the copy at most 10 s old, which runs no patch code (README,
# "API").  Anything the manager itself answers is healthy, the 401 of a container with HRI_PASSWORD set
# included; a refused connection and the 503 the entrypoint serves while Home Assistant installs are not.
# start-period: that install is the slow part, and an install that writes nothing for 15 minutes is the
# longest one the entrypoint waits for (PIP_IDLE_TIMEOUT_S) - 20 minutes leaves the rest of the boot (apt
# packages, the PyPI lookup, the manager's requirements, Home Assistant's own first start) inside it, and
# a probe that fails in there never counts: the first answer flips the container to healthy at once.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20m --retries=3 \
    CMD ["python", "-c", "import http.client, os; c = http.client.HTTPConnection('127.0.0.1', int(os.environ.get('HRI_PORT') or 8087), timeout=5); c.request('GET', '/api/status'); raise SystemExit(0 if c.getresponse().status < 500 else 1)"]

CMD ["python", "/app/entrypoint.py"]
