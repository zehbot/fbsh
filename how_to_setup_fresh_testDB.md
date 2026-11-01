# Setting up a fresh InfluxDB test instance

Purpose: verify a backup archive without touching production. Every command below
is a **single line** — copy, replace the `<…>` values once, run in order.

Assumptions: Docker is installed (as a package or as the Ubuntu snap; the
commands work with both), and `docker` needs `sudo`, as it does on Ubuntu
default installs.

---

## 0. Set the values once

```bash
export INFLUX_TOKEN='***'; export INFLUX_ORG='example_org'; export INFLUX_PASS='<admin-password-min-8-chars>'
```

| Variable | Meaning |
|---|---|
| `INFLUX_TOKEN` | token this test instance will use — **your choice**, any long random string |
| `INFLUX_ORG` | org name for the test instance |
| `INFLUX_PASS` | password for the `admin` user of the test instance |

They live only in this shell. A new terminal starts empty.

---

## 1. Throw away any previous attempt

```bash
sudo docker rm -f influxrestore; sudo docker volume rm influxrestore-data
```

Both parts matter: `docker rm` removes the container, `docker volume rm` removes
its database. Leaving the volume behind is why a "fresh" instance sometimes comes
back with old buckets.

## 2. Prepare the backup directory

```bash
mkdir -p "$HOME/backups/influx"
```

`$HOME` on purpose: if Docker is the snap, a bind mount of `/tmp` silently points
into the snap's private temp directory. `$HOME` is always reachable.

## 3. Start the instance

```bash
sudo docker run -d --name influxrestore -p 8086:8086 -v influxrestore-data:/var/lib/influxdb2 -v "$HOME/backups/influx:/backup:ro" influxdb:2.8
```

Read it as two different kinds of volume:

- `influxrestore-data:/var/lib/influxdb2` — a **named volume**, managed by Docker
  under `/var/lib/docker/volumes/`, survives `docker rm`
- `"$HOME/backups/influx:/backup:ro"` — a **bind mount** of a real directory, read-only

Everything after the image name (`influxdb:2.8`) would be the container's command.
Keep `-v` and `-p` flags before it.

## 4. Wait until it answers

```bash
until curl -sf http://127.0.0.1:8086/health >/dev/null; do sleep 1; done; curl -s http://127.0.0.1:8086/health
```

If this never returns, the container died: `sudo docker logs influxrestore`.

## 5. Initialise it

```bash
sudo docker exec influxrestore influx setup --username admin --password "***" --org "$INFLUX_ORG" --bucket restore_target --retention 0 --token "***" --force
```

This creates the org, one bucket and the **operator token** — the only token that
can later take a backup of this instance.

## 6. Prove the token works

```bash
sudo docker exec -e INFLUX_TOKEN="$INFLUX_TOKEN" influxrestore influx bucket list
```

Expect `restore_target`, `_monitoring`, `_tasks`. Nothing else yet.

## 7. Unpack a backup archive

```bash
sudo docker exec influxrestore sh -c 'cd /tmp && mkdir -p r && tar xzf /backup/<archive>.tar.gz -C r'
```

Replace `<archive>` with the file name in `$HOME/backups/influx`, without the
`.tar.gz` suffix inside the path below.

## 8. Restore

One bucket, under a new name:

```bash
sudo docker exec -e INFLUX_TOKEN="$INFLUX_TOKEN" influxrestore influx restore /tmp/r/<archive>/data --bucket <source-bucket> --new-bucket restored_demo
```

Everything, including users and tokens:

```bash
sudo docker exec -e INFLUX_TOKEN="$INFLUX_TOKEN" influxrestore influx restore /tmp/r/<archive>/data --full
```

`--full` replaces the entire target instance, **including its tokens** — after it,
the token from step 5 no longer exists and you must use the token from the backup.
It also must never point at a live instance.

## 9. Check that the data arrived

```bash
sudo docker exec -e INFLUX_TOKEN="$INFLUX_TOKEN" influxrestore influx query --org "$INFLUX_ORG" 'from(bucket:"restored_demo") |> range(start:0) |> group(columns:["_measurement","_field"]) |> count()'
```

A successful restore prints one `Restoring TSM snapshot for shard N` line per
shard. Shard numbers are not sequential — they restore in parallel — so the last
number says nothing about completeness. The counts from this command do.

## 10. Shut it down

```bash
sudo docker rm -f influxrestore; sudo docker volume rm influxrestore-data
```

A restored instance contains a full copy of the source database, tokens included.
Remove it deliberately rather than remembering later.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| container exits immediately, log mentions the volume name | a stray argument after the image name became the container command |
| `/tmp/...` mount appears empty inside the container | Docker snap with a private `/tmp`; use `$HOME` |
| `401 unauthorized access` after `--full` | the restore replaced the tokens; use the ones from the backup |
| `could not find bucket` | the restore did not create it — check the exit code of step 8 |
| `restore path must be specified as a single positional argument` | broken quoting in the command, usually the token |
| `cp: cannot create regular file … Permission denied` | container runs as uid 10001, target owned by root — `chown` or `--user 0:0` |

---

## Related

- **[howto_influx-bkup.md](howto_influx-bkup.md)** — taking the backups this
  instance is meant to verify, including the operator-token requirement
- **[container_howto.md](container_howto.md)** — running the collector on k3s
- **[howto_import_history.md](howto_import_history.md)** — loading the historic
  CSV export
