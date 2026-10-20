# Stack UI: the web page for the compose stacks of a Podman host.
# podman-remote runs as `podman` (podman-compose takes only that bare name or a full path); CONTAINER_HOST makes
# it talk to the mounted Podman socket. The image runs as root: with a rootful socket it must (the socket is
# root's); with a rootless one, rootless Podman maps root in the container to your user.
FROM python:3.13-slim-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends podman-remote \
    && ln -s /usr/bin/podman-remote /usr/local/bin/podman \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyyaml==6.0.2 podman-compose==1.6.0

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
ENTRYPOINT ["python3", "-m", "stack_ui", "--no-browser"]
