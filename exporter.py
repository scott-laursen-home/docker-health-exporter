#!/usr/bin/env python3
"""Prometheus exporter for Docker container health + run state.

Docker does not expose healthcheck results as metrics, so a container that
hangs while still "running" looks fine to `docker ps`, to `restart:
unless-stopped`, and to every status check. That is exactly how grimmory
failed on 2026-09-10: the JVM deadlocked, the process stayed alive, and
nothing noticed for ~20 minutes. This closes that gap.

Written in-house rather than using a third-party exporter: the available ones
are unmaintained and amd64-only (this host is arm64). stdlib only, no pip
deps, so there is nothing to keep patched.

Exposes on :9060/metrics
  docker_container_health_status{name,status}  1 for the container's current
                                              healthcheck state, 0 for the
                                              others; status=none means the
                                              image declares no healthcheck
  docker_container_running{name}               1 running, 0 otherwise
  docker_container_restart_count{name}         cumulative restarts
  docker_container_start_time_seconds{name}    last start, unix seconds
  docker_health_exporter_up                    0 if the last Docker API poll failed
  docker_health_exporter_containers            containers seen in last poll
  backup_last_success_timestamp_seconds{name}  unix time the nightly backup job
                                              last completed with NO failures;
                                              0 if it has never succeeded
  backup_stamp_found{name}                     1 if the stamp file exists at all
  launchd_plist_drift                          launchd jobs whose installed plist
                                              differs from / is missing from
                                              ~/docker/launchd (-1: never checked)
  launchd_check_timestamp_seconds              unix time of the last drift check

Backup freshness lives here rather than in its own exporter to avoid a second
container, port and scrape job for two gauges. The stamps are written by
~/bin/nightly-dumps.sh and ~/bin/nightly-appdata.sh; see ~/docker/BACKUP.md.

WHY a missing stamp reports timestamp 0 instead of being absent: an alert that
cannot fire when the job has NEVER run is exactly the blind spot that let the
old rsync backup fail 60 nights unnoticed. 0 makes the age astronomical, so the
same "too old" rule catches "never ran" without a second rule.
"""

import http.client
import json
import os
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOCKET_PATH = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
API_VERSION = os.environ.get("DOCKER_API_VERSION", "v1.43")
LISTEN_PORT = int(os.environ.get("PORT", "9060"))
# Scrapes land every 30s; cache so a burst of scrapes cannot stampede the API.
CACHE_TTL = float(os.environ.get("CACHE_TTL_SECONDS", "10"))
# Stamp files: <BACKUP_STAMP_DIR>/.last-success-<name>, each containing a unix
# timestamp. The names are listed explicitly (not globbed) so that a stamp which
# never appears still produces a metric.
BACKUP_STAMP_DIR = os.environ.get("BACKUP_STAMP_DIR", "/backups")
BACKUP_STAMPS = [s for s in os.environ.get("BACKUP_STAMPS", "dumps,volumes").split(",") if s]
HEALTH_STATES = ("healthy", "unhealthy", "starting", "none")


class UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over a unix domain socket (the Docker socket)."""

    def __init__(self, path, timeout=15):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def api_get(path):
    conn = UnixHTTPConnection(SOCKET_PATH)
    try:
        conn.request("GET", "/{}{}".format(API_VERSION, path))
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError("docker api {} -> {} {!r}".format(path, resp.status, body[:160]))
        return json.loads(body)
    finally:
        conn.close()


def escape_label(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
    )


def parse_start_time(value):
    """Docker returns RFC3339 with nanoseconds; trim to microseconds for fromisoformat."""
    if not value or value.startswith("0001-01-01"):
        return 0.0
    text = value.replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        frac = tail
        offset = ""
        for marker in ("+", "-"):
            if marker in frac:
                frac, _, rest = frac.partition(marker)
                offset = marker + rest
                break
        text = "{}.{}{}".format(head, frac[:6], offset)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def collect_backups():
    """Gauges for nightly backup freshness. Never raises: a broken stamp must
    not blank the container metrics sharing this endpoint."""
    stamps, found = [], []
    for name in BACKUP_STAMPS:
        label = escape_label(name)
        ts = 0.0
        exists = 0
        try:
            with open(os.path.join(BACKUP_STAMP_DIR, ".last-success-" + name)) as fh:
                ts = float(fh.read().strip())
                exists = 1
        except Exception:
            ts, exists = 0.0, 0
        stamps.append(
            'backup_last_success_timestamp_seconds{{name="{}"}} {:.0f}'.format(label, ts)
        )
        found.append('backup_stamp_found{{name="{}"}} {}'.format(label, exists))
    return stamps, found


def collect_launchd():
    """launchd plist drift, written by ~/bin/launchd-check.sh as
    "<count> <unixtime>". -1 / 0 when the check has never run, so a check that
    silently stopped running is as visible as drift itself."""
    try:
        with open(os.path.join(BACKUP_STAMP_DIR, ".launchd-drift")) as fh:
            count, ts = fh.read().split()[:2]
            return int(count), float(ts)
    except Exception:
        return -1, 0.0


def collect():
    """Return the Prometheus text exposition for all containers."""
    health = []
    running = []
    restarts = []
    started = []
    ok = 1
    count = 0

    try:
        # all=1 so a container that died outright is still reported (running 0).
        containers = api_get("/containers/json?all=1")
        for item in containers:
            cid = item.get("Id", "")
            if not cid:
                continue
            try:
                info = api_get("/containers/{}/json".format(cid))
            except Exception:
                # One unreadable container must not blank the whole scrape.
                ok = 0
                continue

            name = (info.get("Name") or item.get("Names", ["/?"])[0]).lstrip("/")
            label = escape_label(name)
            state = info.get("State") or {}
            status = ((state.get("Health") or {}).get("Status") or "none").lower()
            if status not in HEALTH_STATES:
                status = "none"

            count += 1
            for candidate in HEALTH_STATES:
                health.append(
                    'docker_container_health_status{{name="{}",status="{}"}} {}'.format(
                        label, candidate, 1 if candidate == status else 0
                    )
                )
            running.append(
                'docker_container_running{{name="{}"}} {}'.format(
                    label, 1 if state.get("Running") else 0
                )
            )
            restarts.append(
                'docker_container_restart_count{{name="{}"}} {}'.format(
                    label, int(state.get("RestartCount") or 0)
                )
            )
            started.append(
                'docker_container_start_time_seconds{{name="{}"}} {:.0f}'.format(
                    label, parse_start_time(state.get("StartedAt"))
                )
            )
    except Exception:
        ok = 0

    backup_stamps, backup_found = collect_backups()
    launchd_drift, launchd_ts = collect_launchd()

    out = [
        "# HELP docker_container_health_status Container healthcheck state (1 = current state).",
        "# TYPE docker_container_health_status gauge",
        *health,
        "# HELP docker_container_running Whether the container is running.",
        "# TYPE docker_container_running gauge",
        *running,
        "# HELP docker_container_restart_count Cumulative restarts of the container.",
        "# TYPE docker_container_restart_count gauge",
        *restarts,
        "# HELP docker_container_start_time_seconds Unix time the container last started.",
        "# TYPE docker_container_start_time_seconds gauge",
        *started,
        "# HELP docker_health_exporter_up Whether the last Docker API poll succeeded.",
        "# TYPE docker_health_exporter_up gauge",
        "docker_health_exporter_up {}".format(ok),
        "# HELP docker_health_exporter_containers Containers seen in the last poll.",
        "# TYPE docker_health_exporter_containers gauge",
        "docker_health_exporter_containers {}".format(count),
        "# HELP backup_last_success_timestamp_seconds Unix time the nightly backup job last fully succeeded (0 = never).",
        "# TYPE backup_last_success_timestamp_seconds gauge",
        *backup_stamps,
        "# HELP backup_stamp_found Whether the backup stamp file exists.",
        "# TYPE backup_stamp_found gauge",
        *backup_found,
        "# HELP launchd_plist_drift launchd jobs whose installed plist differs from or is missing from ~/docker/launchd (-1 = never checked).",
        "# TYPE launchd_plist_drift gauge",
        "launchd_plist_drift {}".format(launchd_drift),
        "# HELP launchd_check_timestamp_seconds Unix time of the last launchd drift check (0 = never).",
        "# TYPE launchd_check_timestamp_seconds gauge",
        "launchd_check_timestamp_seconds {:.0f}".format(launchd_ts),
    ]
    return "\n".join(out) + "\n"


class Cache:
    def __init__(self, ttl):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.at = 0.0
        self.body = ""

    def get(self):
        with self.lock:
            now = time.monotonic()
            if not self.body or now - self.at >= self.ttl:
                self.body = collect()
                self.at = now
            return self.body


CACHE = Cache(CACHE_TTL)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.startswith("/metrics"):
            payload = CACHE.get().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        elif self.path in ("/", "/healthz"):
            payload = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        else:
            payload = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep scrape noise out of docker logs
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("", LISTEN_PORT), Handler).serve_forever()
