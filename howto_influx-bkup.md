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

- **The token must be the operator token from `influx setup`.** Neither a
  bucket-scoped token nor a self-created all-access token can download the
  metadata store; both fail with 401. Details and measurements in 5.2.
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
| `INFLUX_TOKEN` | — (required) | operator token from `influx setup` |
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

### 3.1 Credentials in a file, not on the command line

```bash
mkdir -p ~/.config/fbsh
cat > ~/.config/fbsh/backup.env <<'EOF'
INFLUX_URL=http://192.0.2.20:8086
INFLUX_TOKEN=<operator token>
BACKUP_TARGETS=/backup/a
BACKUP_KEEP_DAYS=3
EOF
chmod 600 ~/.config/fbsh/backup.env
```

Why a file: an inline `-e INFLUX_TOKEN=…` lands in your shell history and is
visible in `ps` for as long as the container runs. `--env-file` keeps it out of
both, and the file's `chmod 600` limits who can read it.

### 3.2 The mount — the part that usually goes wrong

```bash
docker build -t influxdb-backup:0.1.0 ./backup

docker run --rm \
  --env-file ~/.config/fbsh/backup.env \
  -v "$HOME/backups/influx:/backup/a" \
  influxdb-backup:0.1.0
```

`-v HOST:CONTAINER` has two sides, and both matter:

```
        -v /srv/backup-a : /backup/a
           └────┬─────┘    └───┬────┘
        real directory      path inside the container
        on this machine     — must equal an entry of BACKUP_TARGETS
```

The script writes to whatever is in `BACKUP_TARGETS`, default `/backup/a` and
`/backup/b`. If the right-hand side of `-v` differs from that, the container
writes into its own filesystem and the archive disappears when it exits —
`--rm` deletes everything that was not on a mount.

Two targets means two mounts and a matching variable:

```bash
docker run --rm \
  --env-file ~/.config/fbsh/backup.env \
  -v "$HOME/backups/influx:/backup/a" \
  -v /mnt/nas/influx:/backup/b \
  -e BACKUP_TARGETS=/backup/a,/backup/b \
  influxdb-backup:0.1.0
```

| Host path | Mounted at | Purpose |
|---|---|---|
| `$HOME/backups/influx` | `/backup/a` | local disk, fast |
| `/mnt/nas/influx` | `/backup/b` | different machine — survives a dead disk |

The host directories are created if missing, but they must be writable.

#### Trap: the Docker snap and `/tmp`

If Docker is installed as a snap (Ubuntu's default in some releases), the daemon
runs confined with a **private `/tmp`**. A bind mount of `/tmp/backup-a` then
points at the snap's own directory, not yours:

```bash
$ sudo find / -xdev -name 'influxdb-*.tar.gz'
/tmp/snap-private-tmp/snap.docker/tmp/backup-a/influxdb-20261101T174908Z.tar.gz
```

The run reports `copied to /backup/a` and `done`, and the host's `/tmp` stays
empty. `chown` on the host path has no effect, because the container never sees
that path.

Two consequences:

- put backups somewhere persistent — `$HOME/backups/influx` works with the snap,
  `/tmp` is wiped on reboot in any case
- `-v "$HOME/backups/influx:/backup/a"` must be quoted; `-v ~/...` makes Docker
  create a literal `~` directory

```bash
mkdir -p "$HOME/backups/influx"
sudo docker run --rm --env-file ~/.config/fbsh/backup.env \
  -v "$HOME/backups/influx:/backup/a" influxdb-backup:0.1.0
```

Check `snap list | grep docker` if you are unsure which variant you have.

### 3.3 Permissions: uid 10001

The image runs as uid `10001`, not root. A directory created by Docker (or by
`sudo mkdir`) belongs to root with mode 755, so the copy fails even though the
backup itself succeeded:

```
archive: influxdb-20261101T174207Z.tar.gz (19M)
sha256:  f7a7503a...
cp: cannot create regular file '/backup/a/influxdb-....tar.gz': Permission denied
error: target /backup/a is not writable by uid 10001
       fix: sudo chown 10001:10001 "$HOME/backups/influx"
       or start the container with --user "$(id -u):$(id -g)"
```

Note what this means: the snapshot was taken, the archive exists — only the copy
failed. Nothing is lost, but nothing is backed up either. Fix the ownership and
run again; the next run overwrites nothing that matters.

Three ways out, best first:

```bash
# 1. hand the directory to the container's uid
sudo mkdir -p /srv/backup-a && sudo chown 10001:10001 /srv/backup-a

# 2. run with your own uid instead (the image needs no root)
docker run --rm --user "$(id -u):$(id -g)" --env-file ~/.config/fbsh/backup.env \
  -v "$HOME/backups/influx:/backup/a" influxdb-backup:0.1.0

# 3. only for throwaway tests: world-writable
chmod 777 /srv/backup-a
```

`chmod 777` on a directory that holds snapshots of every token in the database
is a bad trade — option 1 or 2.

#### When Docker needs sudo

On many workstations `docker` only runs via `sudo`. Then the bind-mount
directory is created by **root**, and the container — uid 10001 — cannot write
into it, even though the backup itself succeeded:

```
archive: influxdb-20261101T174908Z.tar.gz (19M)
sha256:  8a3c1103c15497b5...
cp: cannot create regular file '/backup/a/influxdb-....tar.gz': Permission denied
error: target /backup/a is not writable by uid 10001
```

Two ways out, and the second one surprises people:

```bash
# a) hand the directory to the container's uid
sudo chown 10001:10001 "$HOME/backups/influx"

# b) run the container as root for this one-off test
sudo docker run --rm --user 0:0 --env-file ~/.config/fbsh/backup.env \
  -v "$HOME/backups/influx:/backup/a" influxdb-backup:0.1.0
```

Note that `--user "$(id -u):$(id -g)"` is **not** a substitute here: under `sudo`
it expands to `0:0`, i.e. the same as option b, and the archives end up owned by
root. Fine for a test run, inconvenient for a directory you want to inspect as
your normal user.

A working run ends with:

```
archive: influxdb-20261101T174908Z.tar.gz (19M)
sha256:  8a3c1103c15497b58177eff91833789aa6072a635eab5dc60e17a8879591630b
copied to /backup/a
done
```

On SELinux systems (Fedora, RHEL, CentOS) add `:z` to the mount so the container
is allowed to write: `-v "$HOME/backups/influx:/backup/a":z`.

### 3.4 Where the container writes besides the mount

`influx backup` needs scratch space under `/tmp` and the script also builds the
tarball there. That is inside the container and vanishes with `--rm`, which is
the point — but it means the dataset must fit twice in the container's temp:

```bash
docker run --rm --read-only --tmpfs /tmp:size=2g \
  --env-file ~/.config/fbsh/backup.env -v "$HOME/backups/influx:/backup/a" \
  influxdb-backup:0.1.0
```

For a few hundred MB of compressed data, 2 GiB of tmpfs is comfortable.

### 3.5 Talking to the database

`INFLUX_URL` inside the file must be reachable **from inside the container**, not
from your shell. `localhost` means the container itself.

```bash
# InfluxDB on the same host: share the host's network
docker run --rm --network host --env-file ~/.config/fbsh/backup.env \
  -v "$HOME/backups/influx:/backup/a" influxdb-backup:0.1.0
# and use INFLUX_URL=http://127.0.0.1:8086

# InfluxDB elsewhere: no network flag needed, just its address
# INFLUX_URL=http://192.0.2.20:8086
```

Without `--network host` you would have to use `--add-host` or the host's bridge
address instead of `127.0.0.1`.

### 3.6 Expected output

```
source:  http://192.0.2.20:8086
targets: /backup/a
keep:    3 archives per target
token:   accepted
running: influx backup
archive: influxdb-20261101T164058Z.tar.gz (8.0K)
sha256:  28f593a97aeaf89f8905a117c8efcc1adeebdf6c4283607842bf7522c849514b
copied to /backup/a
done
```

Check the result and clean up:

```bash
ls -lh "$HOME/backups/influx"
sha256sum -c "$HOME/backups/influx/"*.sha256      # must print OK
rm ~/.config/fbsh/backup.env
```

### 3.7 The same command from the registry

No local build, identical flags:

```bash
docker run --rm --env-file ~/.config/fbsh/backup.env \
  -v "$HOME/backups/influx:/backup/a" \
  inkassodeppen/influxdb-backup:0.1.0
```

| Flag | Why |
|---|---|
| `--rm` | the container is a one-shot; keeps no state |
| `--env-file` | keeps the token out of `ps` and history |
| `-v host:container` | the only place data survives the run |
| `--network host` | only when InfluxDB listens on the same machine |
| `--read-only --tmpfs /tmp` | optional, but the image needs no writable root |
| `--user $(id -u)` | alternative to `chown 10001` — but under `sudo` it resolves to root |
| `--user 0:0` | write as root on purpose, e.g. for a quick local test |

---

## 4. Build and publish for the Pi

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64,arm

docker buildx build --platform linux/amd64,linux/arm64 --provenance=false \
  -t registry.example.org/influxdb-backup:0.1.0 --push ./backup
```

`--provenance=false` keeps the manifest list to the two real platforms; the
attestation otherwise adds a third entry that some tooling trips over.

If `docker buildx` is missing, Docker falls back to the legacy builder and prints
`DEPRECATED: The legacy builder is deprecated`. That is harmless for a
single-architecture build on the machine that will run it:

```bash
docker build -t influxdb-backup:0.1.0 ./backup
```

---

## 5. Deploy on k3s

### 5.1 Prerequisites

- a namespace (`fbsh` in the manifest — adjust freely)
- two PVCs, ideally on **different** storage
- the operator token from `influx setup`

### 5.2 The token — and the trap behind it

Two things are true at once, and the second one is not obvious:

1. the collector's token from `private/.env` cannot be used — it has write access
   to two buckets and nothing else
2. **a normal all-access token does not work either**

A snapshot downloads the metadata store, and InfluxDB only allows that for the
**operator token created by `influx setup`**. Measured on 2.8.0:

| Token | `influx backup` |
|---|---|
| write token (collector) | 401, `read:authorizations is unauthorized` |
| `influx auth create --all-access` | 401, `failed to download metadata snapshot` |
| `influx auth create --operator` | 401, same |
| **operator token from `influx setup`** | **works** |

So the token you need is the one you passed to `--token` when initialising
InfluxDB, or the entry in *Load Data → API Tokens* whose description is
`<username>'s Token`. It cannot be recreated — treat it accordingly.

```bash
# find it: the description is "<user>'s Token"
influx auth list --host http://192.0.2.20:8086 --token ***
```

Then into the Secret, without leaving it in the shell history:

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

### Why a second secret at all

| Token | Stored in | Rights | Used by |
|---|---|---|---|
| collector | `fbsh-env` (from `private/.env`) | write on two buckets | the CronJob that reads the FRITZ!Box |
| backup | `influxdb-backup` | operator | this CronJob only |

They have different lifetimes and different blast radius, so they are separate
secrets with separate rotation. The operator token is the more dangerous one: it
can dump and rewrite the whole instance.

### What a wrong token looks like

The script classifies the failure instead of passing the raw error through:

```
running: influx backup
error: this token may not read the metadata snapshot.
       a snapshot needs the operator token from `influx setup` -
       all-access and self-created operator tokens are rejected.
```

A successful run answers with `token:   accepted` right after the call.

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

A backup that was never restored is a guess. Verify once per quarter, and before
you ever need it in anger.

The commands — fresh container, named volume, setup, unpack, restore, count,
cleanup — are collected in **[how_to_setup_fresh_testDB.md](how_to_setup_fresh_testDB.md)**,
one line per command. This section only covers what matters when reading the
result.

### Two modes, two different outcomes

| Mode | Restores | Bucket names afterwards | Token afterwards |
|---|---|---|---|
| `--bucket X --new-bucket Y` | one bucket's data | `Y`, next to the target's own | your test token keeps working |
| `--full` | the whole instance: data, users, orgs, tokens | **the original names** | **the token from the backup** |

Both are legitimate; they answer different questions. A single bucket proves the
shard data is intact. `--full` proves the whole instance can be resurrected — and
then behaves exactly like production, tokens included:

```
INFO: Restoring KV snapshot
WARN: Restoring KV snapshot overwrote the operator token, ensure following
      commands use the correct token
```

After `--full`, two things look like failures and are not:

- `restored_demo` does not exist — a full restore never creates it, the buckets
  keep their original names
- your own token returns `401` — it was replaced by the snapshot's tokens

`--full` replaces everything on the target, so never point it at a live instance.

### Reading the result

A successful restore lists one `Restoring TSM snapshot for shard N` line per
shard. The numbers are **not sequential** — they restore in parallel, so the last
number printed says nothing about completeness.

The real check is a count:

```flux
from(bucket: "<bucket>")
  |> range(start: 0)
  |> group(columns: ["_measurement", "_field"])
  |> count()
```

Compare it with `check_import.py --diagnose` for the same bucket. Equal counts
per measurement mean the snapshot is faithful; a missing measurement means the
shard carrying it did not come back.

Note what you will see: the instance holds more than this project's data.
Measurements such as `POWER`, `Total_In`, `Total_Out` or `Fritzbox` come from
other producers. A full backup captures them too — the backup is the database,
not one application.

### Two details that cost time

- the flag is **`--new-bucket`**, not `--newBucketName`; `influx restore --help`
  is the source of truth
- a *full* restore (`--full`) needs an empty instance; for verification,
  restoring one bucket is enough and safer

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
- **[how_to_setup_fresh_testDB.md](how_to_setup_fresh_testDB.md)** — a scratch
  instance to restore into, command by command
- **[howto_import_history.md](howto_import_history.md)** — loading the historic
  CSV export into the same bucket
- **[README.md](README.md)** — what `fbsh.py` does
