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

mkdir -p "$PREFIX/booboot_server"
if [ ! -x "$PREFIX/venv/bin/pip" ]; then
    # On Debian, a venv with pip needs the python3-venv package.
    if ! python3 -m venv "$PREFIX/venv" >/dev/null 2>&1; then
        apt-get update
        apt-get install -y python3-venv
        rm -rf "$PREFIX/venv"
        python3 -m venv "$PREFIX/venv"
    fi
fi
"$PREFIX/venv/bin/pip" install --upgrade "usbsdmux>=24.1"

rm -rf "$PREFIX"/booboot_server/*.py "$PREFIX/booboot_server/web"
cp "$SRC"/booboot_server/*.py "$PREFIX/booboot_server/"
cp -r "$SRC/booboot_server/web" "$PREFIX/booboot_server/"
cat > /usr/local/bin/booboot-server <<EOF
#!/bin/sh
PYTHONPATH=$PREFIX exec $PREFIX/venv/bin/python3 -m booboot_server "\$@"
EOF
chmod 755 /usr/local/bin/booboot-server
install -m 755 "$SRC/../client/booboot.py" /usr/local/bin/booboot
# The module for scripts run on this board.
install -m 644 "$SRC/../client/booboot.py" "$PREFIX/booboot.py"

# The version comes from git: the tag, like 0.3.0, or 0.3.0-2-gabc1234 for a later commit.
# git runs as the owner of the clone: it refuses a repository of another user.
REPO=$(cd "$SRC/.." && pwd)
VERSION=$(su "$(stat -c %U "$REPO")" -s /bin/sh -c "git -C '$REPO' describe --tags --always" 2>/dev/null | sed 's/^v//')
case "$VERSION" in
    "" | *[!0-9A-Za-z.+-]*) VERSION=dev ;;
esac
sed -i "s/^__version__ = .*/__version__ = \"$VERSION\"/" "$PREFIX/booboot_server/__init__.py" /usr/local/bin/booboot \
    "$PREFIX/booboot.py"

# Scripts sent by clients run as this user, without access to the hardware.
if ! id -u booboot-script > /dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin booboot-script
fi

# The USB-SD-Mux is controlled through the SCSI generic driver.
echo sg > /etc/modules-load.d/booboot.conf
modprobe sg || true

# Keep desktop automounters away from the SD card of the mux.
mkdir -p /etc/udev/rules.d
install -m 644 "$SRC/99-booboot.rules" /etc/udev/rules.d/
udevadm control --reload || true

mkdir -p /etc/booboot
if [ ! -e "/etc/booboot/$NAME.ini" ]; then
    # One port per DUT: 8080 for the first one, then 8081, ...
    PORT=$((8080 + $(find /etc/booboot -name '*.ini' | wc -l)))
    sed -e "s/^name = .*/name = $NAME/" -e "s/^port = .*/port = $PORT/" \
        "$SRC/booboot.ini" > "/etc/booboot/$NAME.ini"
    echo "Created /etc/booboot/$NAME.ini (port $PORT)"
fi

install -m 644 "$SRC/booboot@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "booboot@$NAME"
systemctl restart "booboot@$NAME"

echo "Service booboot@$NAME started."
echo "Configuration: /etc/booboot/$NAME.ini (then: systemctl restart booboot@$NAME)"
echo "Logs: journalctl -u booboot@$NAME"
