# pinned by digest: a rebuild gets the same base, Dependabot proposes the updates
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

# Home Assistant is NOT baked in: entrypoint.py installs the wanted version
# into /config/venv-<version> (on the volume) at start, so the manager UI
# can update or roll back HA with a restart, and image rebuilds are cheap.
# What a fresh volume installs when it is not told to take the newest, and the fallback when PyPI cannot
# be reached.  A recent, well-tested release - NOT the oldest one that works: that is HA_VERSION_MIN.
ARG HA_VERSION=2026.9.3
# The oldest release this image installs at all; anything older is refused before a change is scheduled,
# and no force lifts it.  2026.5.0 is the oldest measured to work (see README, "Python versions"): older
# releases pin requirements that have no wheel for this image's Python anyway.
ARG HA_VERSION_MIN=2026.5.0
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HRI_CONFIG=/config \
    HRI_PORT=8087 \
    HA_VERSION_DEFAULT=${HA_VERSION} \
    HA_VERSION_MIN=${HA_VERSION_MIN} \
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

# Without this Docker cannot tell whether the container is alive.  There is no curl in the image, so its
# own Python asks HRI_PORT (read at runtime, not the 8087 baked in above) for /api/alive.  Liveness only:
# while Home Assistant installs, the entrypoint's status page answers it 200, so an install is never
# "unhealthy" (the Supervisor's watchdog restarts an unhealthy app, and after a restart in place the start
# period does not apply again); once Home Assistant runs, the manager has no view there and answers 404,
# or 401 with HRI_PASSWORD set - anything below 500 is an answer, so healthy.  A refused connection, a
# timeout or a 5xx are not.  start-period: the time before the status page first listens (the image's
# first steps) and a slow first answer; 20 minutes, as when the probe waited for the install, and a probe
# that fails in there never counts: the first answer flips the container to healthy at once.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20m --retries=3 \
    CMD ["python", "-c", "import http.client, os; c = http.client.HTTPConnection('127.0.0.1', int(os.environ.get('HRI_PORT') or 8087), timeout=5); c.request('GET', '/api/alive'); raise SystemExit(0 if c.getresponse().status < 500 else 1)"]

CMD ["python", "/app/entrypoint.py"]
