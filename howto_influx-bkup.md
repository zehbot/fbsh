# Backing up InfluxDB on k3s

The measurement history exists in exactly one place: an InfluxDB bucket. This
document describes the container that snapshots it nightly to **two independent
targets**, how to run it, and how to prove that a snapshot restores.

Everything lives in `backup/`:

```
backup/Dockerfile    influxdb:2.8 plus one script - no extra tooling
backup/backup.sh     the runner, configured entirely through the environment
backup/k8s.yaml      two PersistentVolumeClaims and a CronJob
```

```
┌──────────────┐   influx backup   ┌─────────────┐
│  InfluxDB    │──────────────────▶│ tar.gz      │
│  (source)    │   HTTP, gzip      │ + .sha256   │
└──────────────┘                   └──────┬──────┘
                                          │ copy
                             ┌────────────┴────────────┐
                             ▼                         ▼
                      /backup/a (PVC)            /backup/b (PVC)
```

---

## 1. What a snapshot contains

`influx backup` is InfluxDB's own snapshot format: the bolt store (metadata,
users, orgs, **tokens**) plus the TSM files for all shards. It is restored with
`influx restore` into any 2.x instance of the same or newer version.

Two consequences worth stating plainly:

- **The token needs all-access rights.** A bucket-scoped write token cannot read
  the metadata, so `influx backup` fails with 401.
- **The archive is as sensitive as a password file.** It contains every token and
  password hash of the server. The script writes mode `600`; whether that is
  enough depends on who can read the volume.

There is no line-protocol export mode. InfluxDB 2.x OSS has no `/api/v2/export`
endpoint (it returns 404), and converting query results to line protocol by hand
would be a second implementation to maintain and verify.

---

## 2. Configuration

All through the environment — no files inside the image, nothing baked in.

| Variable | Default | Meaning |
|---|---|---|
| `INFLUX_URL` | — (required) | source, e.g. `http://influxdb.influxdb.svc.cluster.local:8086` |
| `INFLUX_TOKEN` | — (required) | all-access token |
| `BACKUP_TARGETS` | `/backup/a,/backup/b` | comma separated paths, one copy each |
| `BACKUP_KEEP_DAYS` | `14` | archives kept **per target**, older ones are removed |

Exit codes: `0` ok · `1` at least one target failed, the others remain valid ·
`2` configuration error.

### What one run does

1. `influx backup /tmp/<name>/data --compression gzip`
2. packs it into `/tmp/<name>.tar.gz`
3. copies archive and a `.sha256` sidecar to every target, `chmod 600` both
4. prunes each target down to `BACKUP_KEEP_DAYS` archives
5. logs size and checksum

A failure on one target does not delete anything and does not stop the other
copies — but it does exit `1`, so the CronJob shows up as failed.

---

## 3. Run it locally first

```bash
docker build -t influxdb-backup:0.1.0 ./backup

# credentials in a file, not in the command line (they end up in `ps` and history)
cat > /tmp/backup.env <<'EOF'
INFLUX_URL=http://192.0.2.20:8086
INFLUX_TOKEN=<all-access token>
BACKUP_TARGETS=/backup/a
BACKUP_KEEP_DAYS=3
EOF
chmod 600 /tmp/backup.env

docker run --rm --env-file /tmp/backup.env \
  -v /srv/backup-a:/backup/a \
  influxdb-backup:0.1.0

rm /tmp/backup.env
```

The image also runs straight from the registry, without building — same
`--env-file`, different image name:

```bash
docker run --rm --env-file /tmp/backup.env -v /srv/backup-a:/backup/a \
  inkassodeppen/influxdb-backup:0.1.0
```

Expected output:

```
source:  http://192.0.2.20:8086
targets: /backup/a
keep:    3 archives per target
running: influx backup
archive: influxdb-20261101T164058Z.tar.gz (8.0K)
sha256:  28f593a97aeaf89f8905a117c8efcc1adeebdf6c4283607842bf7522c849514b
copied to /backup/a
done
```

The container runs as uid `10001`. If the bind mount is owned by another user,
the copy fails with `target /backup/a is not writable` — `chown 10001:10001` or
use `-e BACKUP_TARGETS` with a path you own.

---

## 4. Build and publish for the Pi

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64,arm

docker buildx build --platform linux/amd64,linux/arm64 --provenance=false \
  -t registry.example.org/influxdb-backup:0.1.0 --push ./backup
```

`--provenance=false` keeps the manifest list to the two real platforms; the
attestation otherwise adds a third entry that some tooling trips over.

---

## 5. Deploy on k3s

### 5.1 Prerequisites

- a namespace (`fbsh` in the manifest — adjust freely)
- two PVCs, ideally on **different** storage
- an all-access token

### 5.2 The token — where it comes from

The token in `private/.env` (the collector's) is **not** usable here. It has write
access to two buckets; a snapshot reads users, orgs and authorisations, which
requires an **all-access** token.

Create a dedicated one:

**InfluxDB UI** — *Load Data → API Tokens → Generate API Token → All Access*,
description `backup`. The value is shown once.

**or via CLI**, on a machine that already has an operator token:

```bash
influx auth create \
  --all-access \
  --org example_org \
  --description "nightly backup" \
  --host http://192.0.2.20:8086 \
  --token ***
```

Both print the token exactly once. Put it into the Secret without leaving it in
the shell history:

```bash
read -rs BACKUP_TOKEN           # paste, nothing is echoed
kubectl -n fbsh create secret generic influxdb-backup \
  --from-literal=INFLUX_URL=http://influxdb.influxdb.svc.cluster.local:8086 \
  --from-literal=INFLUX_TOKEN="$BACKUP_TOKEN"
unset BACKUP_TOKEN
```

Check what landed — names only, never values:

```bash
kubectl -n fbsh get secret influxdb-backup -o jsonpath='{.data}' | jq -r 'keys[]'
```

### Why a second token at all

| Token | Stored in | Rights | Used by |
|---|---|---|---|
| collector | `fbsh-env` (from `private/.env`) | write on two buckets | the CronJob that reads the FRITZ!Box |
| backup | `influxdb-backup` | all-access | this CronJob only |

Separate secrets mean separate rotation. A leaked write token can corrupt two
buckets; a leaked all-access token can dump and rewrite everything, so it should
exist in exactly one place.

The backup script checks this before the first byte: if the token cannot list
authorisations, it stops with

```
error: the token cannot list authorizations, so it is not all-access.
       create one with: influx auth create --all-access --org <org> --description backup
```

instead of a bare `401` from somewhere inside `influx backup`. A successful run
says so in the log:

```
token:   all-access confirmed
running: influx backup
```

### 5.3 Two targets that are actually independent

`backup/k8s.yaml` creates `influx-backup-a` and `influx-backup-b`, both with
`storageClassName: local-path`. That is only a starting point: two PVCs on the
same disk die together — a failed disk, a deleted namespace or a ransomware run
takes both copies.

Make `influx-backup-b` point somewhere else:

| Medium | `storageClassName` |
|---|---|
| NFS export on a NAS | `nfs-client` (or your provisioner) |
| USB disk on another node | `local-path` with `nodeAffinity`, or a static PV |
| Object storage | not supported directly — sync the directory out afterwards |

The whole dataset compresses to a few hundred MB, small enough to copy a weekly
archive off-site by hand.

### 5.4 Apply

```bash
kubectl apply -f backup/k8s.yaml
kubectl -n fbsh get cronjob influxdb-backup
# NAME              SCHEDULE    SUSPEND   ACTIVE   LAST SCHEDULE
# influxdb-backup   0 3 * * *   False     0        <none>
```

The CronJob runs daily at 03:00, `concurrencyPolicy: Forbid`,
`backoffLimit: 1`, `activeDeadlineSeconds: 1800`. The pod runs as uid 10001 with
`fsGroup: 10001` so the PVCs are writable, a read-only root filesystem, and an
`emptyDir` at `/tmp` because `influx backup` writes there first.

### 5.5 Run it once by hand

```bash
kubectl -n fbsh create job --from=cronjob/influxdb-backup influxdb-backup-now
kubectl -n fbsh logs -l job-name=influxdb-backup-now -f
kubectl -n fbsh delete job influxdb-backup-now
```

---

## 6. Verify

```bash
kubectl -n fbsh get jobs
kubectl -n fbsh describe job -l job-name=influxdb-backup-now | tail -20
```

Check the files on a target — for example from a throwaway pod:

```bash
kubectl -n fbsh run peek --rm -it --image=busybox:1.36 --restart=Never \
  --overrides='{"spec":{"containers":[{"name":"peek","image":"busybox:1.36",
    "command":["sh","-c","ls -lh /backup/a && sha256sum -c /backup/a/*.sha256"],
    "volumeMounts":[{"name":"a","mountPath":"/backup/a"}]}],
    "volumes":[{"name":"a","persistentVolumeClaim":{"claimName":"influx-backup-a"}}]}}' \
  -- sh
```

Expected: archives of increasing age, and `OK` from the checksum.

### Alert on absence, not on failure

A CronJob that stops firing produces no error at all. The reliable check is the
age of the newest archive:

```
newest file in /backup/a older than 36 hours  →  alert
```

Any monitoring that can stat a file, or a tiny exporter sidecar, does the job.

---

## 7. Restore

A backup that was never restored is a guess. Do this once per quarter, and
always before relying on the archive.

```bash
# 1. a scratch instance
docker run -d --name influxrestore -p 8086:8086 influxdb:2.8

# 2. get the archive in
docker cp influxdb-<stamp>.tar.gz influxrestore:/tmp/b.tar.gz
docker exec influxrestore sh -c 'cd /tmp && tar xzf b.tar.gz'

# 3. set the target up (a restore needs a token to talk to the server)
docker exec influxrestore influx setup --username admin --password '***' \
  --org example_org --bucket restore_target --retention 0 \
  --token "$INFLUX_TOKEN" --force

# 4. restore one bucket under a new name
docker exec influxrestore influx restore /tmp/influxdb-<stamp>/data \
  --bucket <original-bucket> --new-bucket restored_demo \
  --host http://127.0.0.1:8086 --token '***'

# 5. count the points and compare
curl -sS --request POST "http://127.0.0.1:8086/api/v2/query?org=example_org" \
  -H "Authorization: Token ***" \
  -H "Content-Type: application/vnd.flux" -H "Accept: application/csv" \
  --data 'from(bucket:"restored_demo") |> range(start: 0) |> filter(fn:(r)=> r._field=="temp") |> count()'
```

Two details that cost me time and are easy to get wrong:

- the flag is **`--new-bucket`**, not `--newBucketName`; `influx restore --help`
  is the source of truth
- a *full* restore (`--full`) replaces everything on the target and needs an
  empty instance — for verification, restoring one bucket is enough and safer

---

## 8. Sizing and retention

| Quantity | Rough value |
|---|---|
| 3 M points, 20 series | a few hundred MB compressed |
| nightly growth at 10-minute resolution | a few MB |
| 14 archives × 2 targets | well inside 20 Gi |

`BACKUP_KEEP_DAYS` counts **archives**, not days — the name is historical. With a
daily schedule the two are the same. If you run the job more often, lower the
value accordingly.

Snapshots are full copies, not incrementals. That is deliberate: a full copy can
be restored on its own, without a chain that must be complete.

---

## 9. Troubleshooting

| Symptom | Cause |
|---|---|
| `error: INFLUX_URL is not set` | Secret not mounted, or the key is misspelled |
| `401 Unauthorized` | token lacks all-access rights — bucket-scoped tokens cannot read metadata |
| `target /backup/a is not writable` | PVC ownership; `fsGroup: 10001` missing, or a bind mount owned by another uid |
| `no space left on device` in `/tmp` | the `emptyDir` is too small for the dataset; raise `sizeLimit` |
| job `Error`, log shows `context deadline exceeded` | dataset grew beyond `activeDeadlineSeconds: 1800` |
| archives present but stale | CronJob suspended, or the controller is not running — check `kubectl get cronjob` |
| restore: `restore path must be specified as a single positional argument` | quoting swallowed the path, or a flag was misspelled |
| restore: `failed to check existence of organization` | the target instance has no matching org; set it up first (step 7.3) |

---

## 10. Related

- **[container_howto.md](container_howto.md)** — the collector's image, Secret,
  CronJob, TLS and day-2 operations
- **[howto_import_history.md](howto_import_history.md)** — loading the historic
  CSV export into the same bucket
- **[README.md](README.md)** — what `fbsh.py` does
