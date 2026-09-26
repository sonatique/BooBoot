#!/bin/sh
# Install or update the BooBoot server on this board. Run as root from the repository:
#   sudo server/install.sh [NAME]
# NAME is the DUT name (default dut1). Its configuration is /etc/booboot/NAME.ini
# and its service is booboot@NAME. Run again with another name for another DUT.
set -eu

NAME=${1:-dut1}
SRC=$(cd "$(dirname "$0")" && pwd)
PREFIX=/opt/booboot

if [ "$(id -u)" != 0 ]; then
    echo "run as root: sudo $0 $*" >&2
    exit 1
fi

if ! python3 -c "import ensurepip" 2>/dev/null; then
    apt-get update
    apt-get install -y python3-venv
fi

mkdir -p "$PREFIX/booboot_server"
[ -x "$PREFIX/venv/bin/python3" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade "usbsdmux>=24.1"

rm -f "$PREFIX"/booboot_server/*.py
cp "$SRC"/booboot_server/*.py "$PREFIX/booboot_server/"
cat > /usr/local/bin/booboot-server <<EOF
#!/bin/sh
PYTHONPATH=$PREFIX exec $PREFIX/venv/bin/python3 -m booboot_server "\$@"
EOF
chmod 755 /usr/local/bin/booboot-server
install -m 755 "$SRC/../client/booboot.py" /usr/local/bin/booboot

# The USB-SD-Mux is controlled through the SCSI generic driver.
echo sg > /etc/modules-load.d/booboot.conf
modprobe sg || true

mkdir -p /etc/booboot
if [ ! -e "/etc/booboot/$NAME.ini" ]; then
    sed "s/^name = .*/name = $NAME/" "$SRC/booboot.ini" > "/etc/booboot/$NAME.ini"
    echo "Created /etc/booboot/$NAME.ini"
fi

install -m 644 "$SRC/booboot@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "booboot@$NAME"
systemctl restart "booboot@$NAME"

echo "Service booboot@$NAME started."
echo "Configuration: /etc/booboot/$NAME.ini (then: systemctl restart booboot@$NAME)"
echo "Logs: journalctl -u booboot@$NAME"
