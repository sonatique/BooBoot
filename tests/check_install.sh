#!/bin/sh
# Checks an installed server (after "sudo server/install.sh") with systemd:
# the service, the client, the relay line, a crash restart and a clean stop.
# The relay must be on the gpio-mockup line given as first argument.
# Used by CI. Needs root for debugfs and systemctl (it uses sudo).
set -eu

LINE=${1:-gpio-mockup-A-3}
URL=http://127.0.0.1:8080
SERVICE=booboot@dut1

fail() {
    echo "FAIL: $*"
    sudo journalctl -u "$SERVICE" --no-pager | tail -40
    exit 1
}

wait_up() {
    for _ in $(seq 40); do
        curl -sf "$URL/api/v1/status" > /dev/null && return 0
        sleep 0.5
    done
    fail "service does not answer"
}

# Physical level of the relay line: hi or lo.
level() {
    sudo grep "$LINE" /sys/kernel/debug/gpio | grep -o -w -E 'hi|lo' | tail -1
}

power() {
    booboot --url "$URL" --json status | python3 -c 'import json, sys; print(json.load(sys.stdin)["power"]["state"])'
}

mountpoint -q /sys/kernel/debug || sudo mount -t debugfs none /sys/kernel/debug

[ "$(systemctl is-enabled "$SERVICE")" = enabled ] || fail "service not enabled"
wait_up
[ "$(power)" = off ] || fail "power not off at start"
[ "$(level)" = lo ] || fail "relay line not low at start"
echo "ok: service up, relay off"

booboot --url "$URL" power on
[ "$(level)" = hi ] || fail "relay line not high after power on"
booboot --url "$URL" power off
[ "$(level)" = lo ] || fail "relay line not low after power off"
echo "ok: relay switched by the client"

booboot --url "$URL" power on
sudo systemctl kill -s KILL "$SERVICE"
sleep 3
wait_up
[ "$(power)" = off ] && [ "$(level)" = lo ] || fail "relay not off after a crash restart"
echo "ok: systemd restarts the service after a crash, relay off"

booboot --url "$URL" power on
sudo systemctl stop "$SERVICE"
sleep 1
sudo journalctl -u "$SERVICE" --no-pager | grep "power " | tail -1 | grep -q "power off" \
    || fail "no power off at stop"
echo "ok: power off at service stop"

sudo systemctl start "$SERVICE"
wait_up
booboot --url "$URL" session close
echo "all install checks passed"
