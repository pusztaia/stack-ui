# Stack UI: the web page for the compose stacks of a Podman host.
# podman-remote runs as `podman` (podman-compose takes only that bare name or a full path); CONTAINER_HOST makes
# it talk to the mounted Podman socket. The image runs as root: with a rootful socket it must (the socket is
# root's); with a rootless one, rootless Podman maps root in the container to your user.
# The base image is pinned by digest (Dependabot proposes the next one); the tag says which release it is.
FROM python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

# requirements.txt lists the exact files (with their hashes) pip may install
COPY requirements.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends podman-remote tini \
    && ln -s /usr/bin/podman-remote /usr/local/bin/podman \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

WORKDIR /app
COPY stack_ui/ ./stack_ui/

ENV STACK_UI_BIND=0.0.0.0 \
    STACK_UI_PORT=8770 \
    STACK_UI_DATA=/data \
    PODMAN_SOCKET=/run/podman/podman.sock \
    CONTAINER_HOST=unix:///run/podman/podman.sock \
    PODMAN_PATH=podman \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8770
# tini is PID 1: it reaps the processes a cancelled podman-compose leaves behind, and passes signals on
ENTRYPOINT ["tini", "--", "python3", "-m", "stack_ui", "--no-browser"]
