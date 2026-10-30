# syntax=docker/dockerfile:1
#
# Runtime for the FRITZ!Box client. Works on amd64, arm64 and arm/v7 (Raspberry Pi).
# No pip install: the package is pure standard library, so the image stays small
# and builds fast even under emulation.
#
#   docker build -t hase10smarthome:0.1.0 .
#   docker run --rm --env-file private/.env hase10smarthome:0.1.0 devices

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    HASE10_ENV_FILE=/run/hase10/.env

WORKDIR /app

COPY hase10/ ./hase10/
COPY pyproject.toml README.md ./

RUN useradd --uid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin hase10

USER 10001
WORKDIR /app

ENTRYPOINT ["python", "-m", "hase10"]
CMD ["watch", "--interval", "60"]
