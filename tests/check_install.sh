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
# install.sh sets the version from git, the same for the server and the client.
VERSION=$(booboot --version)
[ "$VERSION" != dev ] || fail "version not set by install.sh"
[ "$(booboot --url "$URL" --json status | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])')" = "$VERSION" ] \
    || fail "server version is not $VERSION"
[ "$(power)" = off ] || fail "power not off at start"
[ "$(level)" = lo ] || fail "relay line not low at start"
echo "ok: service up, relay off"

curl -sf "$URL/" | grep -q 'src="console.js"' || fail "no console web page"
curl -sf "$URL/console.js" > /dev/null || fail "no console.js"
echo "ok: console web page"

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

booboot --url "$URL" script list 2>&1 | grep -q "scripts are off" || fail "scripts not off by default"
sudo sed -i 's/^enabled = no$/enabled = yes/' /etc/booboot/dut1.ini
sudo systemctl restart "$SERVICE"
wait_up
SCRIPT=$(mktemp --suffix .py)
cat > "$SCRIPT" <<'END'
import glob, os, pwd, booboot
print(pwd.getpwuid(os.getuid()).pw_name)
for path in glob.glob("/dev/gpiochip*") + ["/opt/booboot/x"]:
    try:
        open(path, "ab")
        print("opened", path)
    except OSError:
        pass
print(booboot.Client.from_env().power_off()["power"])
END
OUT=$(booboot --url "$URL" script run "$SCRIPT")
[ "$OUT" = "booboot-script
off" ] || fail "script output: $OUT"
echo "ok: scripts run as booboot-script, without access to the hardware"
cat > "$SCRIPT" <<'END'
import os
first = os.path.join(os.path.dirname(os.path.dirname(os.getcwd())), "1")
for read in (lambda: open(os.path.join(first, "info.json")), lambda: os.listdir(os.path.join(first, "work"))):
    try:
        read()
        print("read")
    except OSError:
        print("refused")
END
OUT=$(booboot --url "$URL" script run "$SCRIPT")
[ "$OUT" = "refused
refused" ] || fail "script read the files of another script: $OUT"
echo "ok: scripts cannot read the files of other scripts"

booboot --url "$URL" session close
echo "all install checks passed"
