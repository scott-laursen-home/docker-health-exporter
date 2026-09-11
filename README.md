# docker-health-exporter (+ deadman)

Two small pieces of a homelab monitoring chain, published because the gap they
fill is common and the existing answers are either unmaintained or amd64-only.

- **`exporter.py`** — a Prometheus exporter for Docker container *health*, not
  just run state. Python stdlib only, no pip dependencies, runs from the
  stock `python:3.13-alpine` image on any architecture.
- **`deadman.sh`** — a conditional dead-man's switch for
  [healthchecks.io](https://healthchecks.io) that only pings while the
  monitoring stack itself can raise alerts.

## Why

`docker ps`, `restart: unless-stopped` and every "is it running" check look at
the process. A container can deadlock with its process alive and stay
"running" forever. Docker's own healthchecks catch that, but Docker exposes
the result nowhere a metric can see it. This exporter reads the Docker API and
turns healthcheck state into gauges, so one alert rule covers every container
that declares a healthcheck.

The second half of the problem: Prometheus, Grafana and this exporter all run
on the machine they watch. If that machine dies, they do not fire — they go
silent, and silence looks exactly like health. `deadman.sh` pings an external
service, but only after verifying Grafana and Prometheus answer, so the ping
means "the thing that would warn you is able to", not merely "the box has
power".

## Metrics (`:9060/metrics`)

| Metric | Meaning |
|---|---|
| `docker_container_health_status{name,status}` | 1 for the container's current state (`healthy`, `unhealthy`, `starting`, `none`), 0 for the others. `none` = the image declares no healthcheck |
| `docker_container_running{name}` | 1 running, 0 otherwise. Stopped containers are still reported so they can alert |
| `docker_container_restart_count{name}` | cumulative restarts |
| `docker_container_start_time_seconds{name}` | last start, unix seconds |
| `docker_health_exporter_up` | 0 if the last Docker API poll failed |
| `docker_health_exporter_containers` | containers seen in the last poll |
| `backup_last_success_timestamp_seconds{name}` | optional, see below |
| `backup_stamp_found{name}` | optional, see below |
| `launchd_plist_drift`, `launchd_check_timestamp_seconds` | optional, macOS only, see below |

Design choices worth knowing about:

- **A container without a healthcheck is invisible to the "unhealthy" alert.**
  `docker_container_running` catches a crash but never a hang. Alert on
  `status="none"` if you want to be told about blind spots.
- **One unreadable container does not blank the scrape.** The exporter keeps
  going and sets `docker_health_exporter_up` to 0.
- **Responses are cached** (`CACHE_TTL_SECONDS`, default 10s) so a burst of
  scrapes cannot stampede the Docker API.

### Backup stamps (optional)

If your backup jobs write a unix timestamp into `<dir>/.last-success-<name>`
on full success, mount that directory at `/backups` and list the names in
`BACKUP_STAMPS`. A missing stamp reports timestamp **0**, not absence, on
purpose: `time() - 0` is astronomical, so "the job has never run" trips the
same staleness rule as "the job stopped running". An alert that cannot fire
when the job never ran is the blind spot that let a backup fail unnoticed for
60 nights on the machine this was written for.

### launchd drift (optional, macOS)

Reads `/backups/.launchd-drift` (`"<count> <unixtime>"`) if present. Written
by a nightly check that compares versioned launchd plists against the
installed copies. Ignore it if you are not on macOS.

### Textfile collector (optional)

Set `TEXTFILE_DIR` to a directory (mounted read-only) and every `*.prom` file
in it is appended to the output verbatim, the same contract as node_exporter's
textfile collector. Use it for one-shot host jobs that have a number to report
but no process to scrape -- a backup job writing its repository size, for
example. Include `# HELP` / `# TYPE` lines, and write the file atomically
(temp name, then `mv`) so a scrape never sees half a file. Unreadable files
are skipped, never fatal.

## Alert rules that work

PromQL as used with Grafana alerting; thresholds in parentheses.

| Rule | Expression | Fires when |
|---|---|---|
| container unhealthy | `docker_container_health_status{status="unhealthy"}` | > 0 for 2m |
| container down | `docker_container_running` | < 1 for 2m |
| health detection is blind | `docker_health_exporter_up` | < 1 for 5m, and **noDataState: Alerting** |
| nightly backup is stale | `time() - backup_last_success_timestamp_seconds` | > 93600 (26h) for 15m, noDataState: Alerting |

`noDataState: Alerting` on the last two matters: if the exporter itself dies,
that is also a failure of visibility and should be noticed.

## deadman.sh

Runs in a `curlimages/curl` container inside the same compose project as
Grafana and Prometheus, so it reaches them by service name. Every
`CHECK_INTERVAL` seconds it checks `http://grafana:3000/api/health` and
`http://prometheus:9090/-/healthy`; if both answer it pings `HC_PING_URL`.
After `FAIL_THRESHOLD` consecutive failures it pings `HC_PING_URL/fail`,
which bypasses the healthchecks.io grace period and alerts immediately.

The threshold exists because at boot this container is easily up before
Grafana is; firing `/fail` on the first miss would page on every reboot.
Transient states resolve through the normal grace period instead.

Configure the healthchecks.io check with a period matching `CHECK_INTERVAL`
and a grace period longer than one interval.

## Install

See `compose.example.yaml` and `prometheus.example.yml`. Copy `exporter.py`
and `deadman.sh` next to your monitoring compose file, add the two services,
set `HC_PING_URL` in that stack's `.env`, and add the scrape job.

## Security note

The exporter mounts `docker.sock`. `:ro` protects the socket *file*, not the
API: anything with the socket has root-equivalent control of the daemon. The
exporter only issues GET requests, but that is behaviour, not enforcement.
Put a docker-socket-proxy in front if that matters to you.

## License

MIT.
