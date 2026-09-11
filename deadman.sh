#!/bin/sh
# Dead-man's switch: tells an EXTERNAL service (healthchecks.io) that this
# homelab is alive. If the pings stop, healthchecks.io raises the alarm.
#
# WHY THIS EXISTS: Prometheus, Grafana, docker-health-exporter and every alert
# rule all run on the machine they monitor. If the Mac dies, OrbStack crashes or
# the power goes, they do not fire warnings -- they go SILENT, and silence is
# indistinguishable from "everything is fine". Nothing outside this box was
# watching it until 2026-09-10.
#
# The ping is CONDITIONAL on purpose. An unconditional heartbeat only proves the
# machine has power; it would keep pinging happily while Grafana was dead and no
# alert could reach anyone. Checking Grafana + Prometheus first upgrades the
# meaning to "the thing that would tell you about problems is able to".
#
# WHY /fail IS RATE-LIMITED: /fail bypasses the healthchecks.io grace period and
# alerts instantly. At boot this container can easily be up before Grafana is,
# so firing on the first failed check would page on every reboot. Requiring
# FAIL_THRESHOLD consecutive failures (~15 min) means transient states resolve
# through the grace period instead, and /fail is reserved for a monitoring stack
# that is genuinely stuck.
set -u

INTERVAL="${CHECK_INTERVAL:-300}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-3}"
fails=0

# Never let a ping failure kill the loop: no internet must not stop the watcher.
ping_hc() { curl -fsS -m 10 --retry 2 -o /dev/null "${HC_PING_URL}$1" || true; }

# Service names, not host.docker.internal: same compose project, same network.
while true; do
  if curl -fsS -m 10 -o /dev/null http://grafana:3000/api/health \
     && curl -fsS -m 10 -o /dev/null http://prometheus:9090/-/healthy; then
    fails=0
    ping_hc ""
  else
    fails=$((fails + 1))
    [ "$fails" -ge "$FAIL_THRESHOLD" ] && ping_hc "/fail"
  fi
  sleep "$INTERVAL"
done
