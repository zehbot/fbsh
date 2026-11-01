#!/bin/sh
# InfluxDB 2.x snapshot backup, written to several target paths.
#
# Configured entirely through the environment:
#
#   INFLUX_URL        http://influxdb:8086      source
#   INFLUX_TOKEN      operator token            see the note below
#   BACKUP_TARGETS    /backup/a,/backup/b       one path per copy
#   BACKUP_KEEP_DAYS  14                        archives kept per target
#
# The token must be the operator token created by `influx setup`. A snapshot
# reads the metadata store, and InfluxDB rejects everything else there - an
# all-access token passes every other check and still fails with 401.
#
# The archive contains the bolt store including users and tokens - treat it like
# a password file.
#
# Exit codes: 0 ok, 1 backup or target failed, 2 configuration or token problem.

set -eu

TARGETS="${BACKUP_TARGETS:-/backup/a,/backup/b}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="influxdb-${STAMP}"
WORK="/tmp/${NAME}"

log() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 2; }

[ -n "${INFLUX_URL:-}" ] || die "INFLUX_URL is not set"
[ -n "${INFLUX_TOKEN:-}" ] || die "INFLUX_TOKEN is not set"

log "source:  ${INFLUX_URL}"
log "targets: ${TARGETS}"
log "keep:    ${KEEP_DAYS} archives per target"

mkdir -p "${WORK}/data"
log "running: influx backup"

# Classify the failure: a token that cannot read the metadata store produces a
# bare 401 deep inside the backup call, which is useless on its own.
if ! influx backup "${WORK}/data" \
      --host "${INFLUX_URL}" \
      --token "${INFLUX_TOKEN}" \
      --compression gzip >/dev/null 2>"${WORK}/err"; then
  if grep -qi 'unauthorized' "${WORK}/err"; then
    printf 'error: this token may not read the metadata snapshot.\n' >&2
    printf '       a snapshot needs the operator token from `influx setup` -\n' >&2
    printf '       all-access and self-created operator tokens are rejected.\n' >&2
  else
    sed 's/^/  /' "${WORK}/err" >&2
  fi
  exit 2
fi
log "token:   accepted"

ARCHIVE="/tmp/${NAME}.tar.gz"
tar -czf "${ARCHIVE}" -C /tmp "${NAME}"
SUM="$(sha256sum "${ARCHIVE}" | cut -d' ' -f1)"
SIZE="$(du -h "${ARCHIVE}" | cut -f1)"
log "archive: ${NAME}.tar.gz (${SIZE})"
log "sha256:  ${SUM}"

failed=0
for target in $(printf '%s' "${TARGETS}" | tr ',' ' '); do
  if mkdir -p "${target}" 2>/dev/null && cp "${ARCHIVE}" "${target}/"; then
    printf '%s  %s.tar.gz\n' "${SUM}" "${NAME}" > "${target}/${NAME}.tar.gz.sha256"
    chmod 600 "${target}/${NAME}.tar.gz" "${target}/${NAME}.tar.gz.sha256"
    find "${target}" -maxdepth 1 -name 'influxdb-*.tar.gz' -type f \
      | sort -r | tail -n +$((KEEP_DAYS + 1)) | while read -r old; do
          log "pruning: $(basename "${old}")"
          rm -f "${old}" "${old}.sha256"
        done
    log "copied to ${target}"
  else
    printf 'error: target %s is not writable by uid %s\n' "${target}" "$(id -u)" >&2
    printf '       fix: sudo mkdir -p %s && sudo chown %s:%s %s\n' \
      "${target}" "$(id -u)" "$(id -g)" "${target}" >&2
    printf '       or start the container with --user "$(id -u):$(id -g)"\n' >&2
    failed=1
  fi
done

rm -rf "${WORK}" "${ARCHIVE}"

if [ "${failed}" -ne 0 ]; then
  printf 'error: at least one target failed - the other copies are still valid\n' >&2
  exit 1
fi
log "done"
