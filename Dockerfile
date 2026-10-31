# syntax=docker/dockerfile:1
# fbsh.py is a single standard-library file - no build step, no pip.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY fbsh.py ./

USER 10001
ENTRYPOINT ["python", "fbsh.py"]
CMD ["status", "--json"]
