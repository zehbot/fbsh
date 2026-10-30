# syntax=docker/dockerfile:1
#
# Runtime for the FRITZ!Box client. Works on amd64, arm64 and arm/v7 (Raspberry Pi).
# No pip install: the package is pure standard library, so the image stays small
# and builds fast even under emulation.
#
#   docker build -t fbsh:0.1.0 .
#   docker run --rm --env-file private/.env fbsh:0.1.0 devices

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    FBSH_ENV_FILE=/run/fbsh/.env

WORKDIR /app

COPY fbsh/ ./fbsh/
COPY pyproject.toml README.md ./

RUN useradd --uid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin fbsh

USER 10001
WORKDIR /app

ENTRYPOINT ["python", "-m", "fbsh"]
CMD ["watch", "--interval", "60"]
