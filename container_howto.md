# Running fbsh in containers

`fbsh.py` is one file without dependencies, so its image is a single `COPY` on
top of `python:3.12-slim`. This document covers everything around that image:
building and publishing it, choosing a registry, running it on a Raspberry Pi,
scheduling it as a Kubernetes CronJob, and keeping it running.

In the cluster it runs as a **CronJob**: every ten minutes a pod starts, reads
all devices from the FRITZ!Box, writes one point per device to InfluxDB and
exits — no long running process, no state, no persistent volume.

Nothing here is k3s-specific except the manifest in `k8s/`. The same image runs
with `docker run` on a laptop, under Docker Compose, or on any other orchestrator.

```
┌──────────────┐   AHA-HTTP    ┌─────────────────┐   line protocol   ┌───────────┐
│  FRITZ!Box   │◀─────────────▶│ fbsh pod (10 m) │──────────────────▶│ InfluxDB  │
└──────────────┘   port 80     └─────────────────┘   /api/v2/write   └───────────┘
```

---

## 1. What gets deployed

| Object | Name | Purpose |
|---|---|---|
| Namespace | `fbsh` | isolation |
| Secret | `fbsh-env` | `FRITZ_*` and `INFLUX_*` values as environment variables |
| CronJob | `fbsh-collect` | schedule `*/10 * * * *`, one pod per run |

Properties of the pod:

- image contains one file (`fbsh.py`), no pip, no build step
- runs as uid `10001`, non-root
- `readOnlyRootFilesystem: true`, all capabilities dropped, no privilege escalation
- 10 m CPU / 32 Mi memory requested, 96 Mi limit — on a Pi 4 a run takes well under a second
- no liveness or readiness probes on purpose: every probe would log into the FRITZ!Box
- `concurrencyPolicy: Forbid` — a slow run is never overlapped by the next tick

---

## 2. Prerequisites

Sections 3 and 8 apply to any container runtime; sections 4–7 and 9–11 are the
Kubernetes path. Docker alone is enough for the first ones.

| Requirement | Check |
|---|---|
| Docker (for building and for `docker run`) | `docker buildx version` |
| k3s running, `kubectl` configured | `kubectl get nodes` |
| containerd reachable on the node | `sudo k3s ctr version` |
| FRITZ!Box reachable from the container host | `curl -s -o /dev/null -w '%{http_code}\n' http://192.0.2.10/` |
| InfluxDB 2.x reachable from the container host | `curl -s http://192.0.2.20:8086/health` |
| InfluxDB write token and bucket | see step 4 |

The FRITZ!Box needs a user with the **Smart Home** right and *Zugriff für
Anwendungen zulassen* enabled — same as for local use, see the README.

---

## 3. Get the image

### 3.1 Already published (default)

`inkassodeppen/fbsh:0.1.0` is public on Docker Hub as a manifest list:

```
inkassodeppen/fbsh:0.1.0   (also :latest)
├── linux/amd64
└── linux/arm64             ← Raspberry Pi 4/5 with 64-bit OS / k3s
digest sha256:f229d1fddf7d056220487e366cf33d97aa17e7ec0b874ccc17292586ed11d6aa
```

Nothing to build. Run it directly:

```bash
docker run --rm --env-file private/.env inkassodeppen/fbsh:0.1.0 status
```

or continue with step 4 for the Kubernetes path. To pin what runs instead of
following the tag:

```bash
image: inkassodeppen/fbsh@sha256:f229d1fddf7d056220487e366cf33d97aa17e7ec0b874ccc17292586ed11d6aa
```

The other routes below exist for the cases where you change the code, use a
private registry, or run a cluster without one.

### 3.2 Build it yourself, multi-arch

Same result as the published image, from your own source:

```bash
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false \
  -t registry.example.org/fbsh:0.1.0 --push .
```

Note `--provenance=false`: the provenance attestation otherwise adds an extra
manifest entry that some `imagetools inspect` output and older runtimes trip over.

### 3.3 Build natively on the Pi (fast local loop)

```bash
git clone git@github.com:zehbot/fbsh.git
cd fbsh
docker build -t fbsh:0.1.0 .
```

Takes seconds, the image is a `python:3.12-slim` plus one file (~145 MB).

### 3.4 Without a registry

A homelab cluster can import the image directly — no registry, no pull secret:

```bash
# on the machine that built it
docker save fbsh:0.1.0 | ssh pi@192.0.2.30 'sudo k3s ctr images import -'

# verify on the node
ssh pi@192.0.2.30 'sudo k3s ctr images ls | grep fbsh'
```

Two consequences:

- the manifest must reference the same tag (`fbsh:0.1.0`), not a registry path
- use `imagePullPolicy: IfNotPresent` (the default for a fixed tag) **and a new
  tag for every rebuild** — k3s will not re-import an image it already has under
  the same name. `fbsh:0.1.0` → `fbsh:0.1.1` avoids the classic "I changed the
  code and nothing happened".

---

## 4. Create the Secret

The Secret carries the same keys your `private/.env` holds. kubectl reads
`KEY=value` pairs from a file; comments and blank lines are safest stripped first.

The namespace must exist before the Secret — otherwise every scheduled run fails
with `secret "fbsh-env" not found`:

```bash
cd fbsh
kubectl create namespace fbsh --dry-run=client -o yaml | kubectl apply -f -

umask 077
grep -vE '^[[:space:]]*(#|$)' private/.env > /tmp/fbsh.env
kubectl -n fbsh create secret generic fbsh-env \
  --from-env-file=/tmp/fbsh.env --dry-run=client -o yaml | kubectl apply -f -
rm -f /tmp/fbsh.env
```

The namespace step is idempotent; applying `k8s/cronjob.yaml` in the next step
simply re-asserts it.

Expected keys:

```
FRITZ_HOST  FRITZ_USER  FRITZ_PASSWORD
INFLUX_URL  INFLUX_ORG  INFLUX_BUCKET  INFLUX_TOKEN
INFLUX_BUCKET_CONSUMPTION        (optional, second bucket for power)
```

Check the names without revealing the values:

```bash
kubectl -n fbsh get secret fbsh-env -o jsonpath='{.data}' | jq -r 'keys[]'
```

> Never `kubectl get secret … -o yaml` while screen sharing — the values are
> base64, which is plaintext for anyone who can read it.

---

## 5. Apply the CronJob

Now that namespace and Secret exist, apply the schedule. The manifest already
points at the published image, so nothing needs editing:

```bash
kubectl apply -f k8s/cronjob.yaml
```

Only if you use your own registry or a locally imported image:

```bash
sed -i 's|image: inkassodeppen/fbsh:0.1.0|image: registry.example.org/fbsh:0.1.0|' k8s/cronjob.yaml
```

```bash
kubectl -n fbsh get cronjob fbsh-collect
# NAME           SCHEDULE      SUSPEND   ACTIVE   LAST SCHEDULE   AGE
# fbsh-collect   */10 * * * *  False     0        <none>          5s
```

---

## 6. First run

Do not wait for the first tick — start a job by hand:

```bash
kubectl -n fbsh create job --from=cronjob/fbsh-collect fbsh-now
kubectl -n fbsh logs -l job-name=fbsh-now -f
```

Expected:

```
wrote 17 points to smarthome
wrote 7 points to smarthome-power     # only with INFLUX_BUCKET_CONSUMPTION
```

Then clean up the manual job so it does not linger:

```bash
kubectl -n fbsh delete job fbsh-now
```

### Reading the exit code

| Exit | Meaning | Where to look |
|---|---|---|
| `0` | written | — |
| `1` | device or command error, or InfluxDB rejected the write | log line starts with `error:` |
| `2` | configuration incomplete (missing `INFLUX_*`, unreadable `.env`) | the message names the key |
| `3` | login or network error towards the FRITZ!Box | `error: cannot reach …` / `the box rejected …` |

```bash
kubectl -n fbsh get jobs
kubectl -n fbsh describe job -l job-name=fbsh-now
```

`LAST SCHEDULE` and `job.batch/… failed` in `kubectl get events -n fbsh` tell you
whether the schedule is firing at all.

---

## 7. Verify in InfluxDB

Influx UI → Data Explorer, or via the API:

```flux
from(bucket: "smarthome")
  |> range(start: -30m)
  |> filter(fn: (r) => r["_measurement"] == "ExampleRoom01")
  |> last()
```

What to look for:

- `temp` as **double**, `battery` as double, `power` on sockets
- **one** point per run, not two
- timestamps within the last ten minutes

A quick CLI check without the UI:

```bash
set -a; . private/.env; set +a
curl -sS --request POST "$INFLUX_URL/api/v2/query?org=$INFLUX_ORG" \
  -H "Authorization: Token $INFLUX_TOKEN" \
  -H "Content-Type: application/vnd.flux" \
  --data 'from(bucket: "'"$INFLUX_BUCKET"'") |> range(start: -20m) |> last()' \
  | head -20
```

---

## 8. Networking

### InfluxDB inside the same cluster

Use the service DNS name, which needs no DNS zone and no TLS:

```
INFLUX_URL=http://influxdb.influxdb.svc.cluster.local:8086
```

### InfluxDB on the LAN

Use the IP or a name the node can resolve:

```
INFLUX_URL=http://192.0.2.20:8086
```

Pods reach the LAN through the node's masquerade rule, so no special
configuration is needed. If InfluxDB listens on `127.0.0.1` only, pods will get
`connection refused` — it must bind `0.0.0.0` or the LAN address.

### FRITZ!Box

`FRITZ_HOST` must be the box's IP (`192.0.2.10`), not `fritz.box`: the name is
resolved by the router's DNS via DHCP, which a pod does not inherit from the
node in every setup. The IP always works.

### TLS

| Situation | Setting |
|---|---|
| Influx behind a valid certificate | nothing, verification is on by default |
| self-signed certificate | `INFLUX_INSECURE=1` in the Secret |
| internal CA | `INFLUX_INSECURE=0` plus the CA in the image, or terminate TLS at an ingress |

`INFLUX_INSECURE` affects InfluxDB only; the FRITZ!Box connection has its own
`FRITZ_SCHEME` / `FRITZ_VERIFY_TLS` pair.

---

## 9. Day-2 operations

### Change the interval

```bash
kubectl -n fbsh patch cronjob fbsh-collect --type merge \
  -p '{"spec":{"schedule":"*/5 * * * *"}}'
```

Keep it at 10 minutes unless you need resolution: the FRITZ!Box updates its
sensor values roughly every two minutes anyway, and every run is a login.

### Pause without deleting

```bash
kubectl -n fbsh patch cronjob fbsh-collect --type merge -p '{"spec":{"suspend":true}}'
kubectl -n fbsh patch cronjob fbsh-collect --type merge -p '{"spec":{"suspend":false}}'
```

### Deploy a new version

```bash
docker build -t registry.example.org/fbsh:0.1.1 .
docker push registry.example.org/fbsh:0.1.1
sed -i 's|fbsh:0.1.0|fbsh:0.1.1|' k8s/cronjob.yaml
kubectl apply -f k8s/cronjob.yaml
```

Existing Jobs keep the old image; the next scheduled run uses the new one. With
`IfNotPresent` and a reused tag, nothing changes — always bump the tag.

### Rotate the token

```bash
kubectl -n fbsh create secret generic fbsh-env \
  --from-env-file=/tmp/fbsh.env --dry-run=client -o yaml | kubectl apply -f -
```

The next pod picks it up automatically — no restart needed, because the
environment is read at container start.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ImagePullBackOff` | wrong registry path, missing pull secret, image not pushed | `kubectl -n fbsh describe pod …` shows the exact pull error |
| `ErrImageNeverPull` | `imagePullPolicy: Never` but the image is not in containerd | import it (3.3) |
| job `Error`, log `cannot reach fritz.box` | DNS inside the pod | use the box IP in `FRITZ_HOST` |
| `the box rejected the user/password combination` | wrong user, missing Smart Home right, *Zugriff für Anwendungen* off | fix on the box |
| `login blocked for 300 s` | too many failed attempts | wait, or log in once through the web UI |
| `error: cannot reach influx.example.org` | DNS in the pod | use the service name or the IP |
| `HTTP 401 unauthorized access` | token lacks write permission on the bucket | issue a token with write access to that bucket |
| `HTTP 404` | wrong `INFLUX_ORG` or `INFLUX_BUCKET` | check both, they are case-sensitive |
| `HTTP 422 field type conflict` | a field was written once as integer | the series must keep its type; check for an older writer |
| points appear with a wrong time | node clock drift | `timedatectl` on the node, NTP must be active |
| `wrote 0 points` never happens | — | the script refuses to write an empty batch and exits 1 instead |

Useful one-liners:

```bash
kubectl -n fbsh get cronjob,pods,jobs
kubectl -n fbsh logs -l job-name=fbsh-now --tail=50
kubectl -n fbsh get events --sort-by=.lastTimestamp | tail -20
kubectl -n fbsh run nettest --rm -it --image=busybox:1.36 --restart=Never \
  -- nslookup influxdb.influxdb.svc.cluster.local
```

---

## 11. Uninstall

```bash
kubectl delete -f k8s/cronjob.yaml          # CronJob only, keeps the namespace
kubectl delete namespace fbsh               # everything, including the Secret
```

The data stays in InfluxDB — deleting the CronJob never touches the bucket.

---

## 12. Security checklist

- [ ] image contains no credentials; `private/` is excluded via `.dockerignore`
- [ ] Secret created from a file with `umask 077`, temp file removed afterwards
- [ ] pod runs as uid 10001, non-root, read-only root filesystem, capabilities dropped
- [ ] `INFLUX_TOKEN` has write access to one bucket only, not all-access
- [ ] FRITZ!Box user has the Smart Home right only, not "FRITZ!Box settings"
- [ ] no `kubectl get secret -o yaml` in shared terminals
- [ ] optional: a NetworkPolicy restricting egress to the box and InfluxDB
