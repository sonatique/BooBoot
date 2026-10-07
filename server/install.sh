#!/bin/sh
# Install or update the BooBoot server on this board. Run as root from the repository:
#   sudo server/install.sh [NAME]
# One service, booboot, runs all the DUTs of the board. The configuration is
# /etc/booboot/server.ini for the board, and /etc/booboot/NAME.ini for each DUT.
# NAME adds a DUT of that name. Without it, the first install adds dut1.
set -eu

NAME=${1:-}
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
if [ ! -e /etc/booboot/server.ini ]; then
    install -m 644 "$SRC/server.ini" /etc/booboot/server.ini
    # Before version 0.5, each DUT file had the address and the web setting of its server:
    # the board takes those of the first DUT, which keeps its address.
    FIRST=$(find /etc/booboot -maxdepth 1 -name '*.ini' ! -name server.ini | LC_ALL=C sort | head -1)
    if [ -n "$FIRST" ]; then
        for KEY in host port web; do
            VALUE=$(sed -n '/^\[server\]/,/^\[/s/^'"$KEY"' *= *\([0-9A-Za-z.:-]*\) *$/\1/p' "$FIRST" | head -1)
            if [ -n "$VALUE" ]; then
                sed -i "s/^$KEY = .*/$KEY = $VALUE/" /etc/booboot/server.ini
            fi
        done
    fi
    echo "Created /etc/booboot/server.ini"
fi
if [ -z "$NAME" ] && [ -z "$(find /etc/booboot -maxdepth 1 -name '*.ini' ! -name server.ini)" ]; then
    NAME=dut1
fi
if [ -n "$NAME" ] && [ ! -e "/etc/booboot/$NAME.ini" ]; then
    sed -e "s/^name = .*/name = $NAME/" "$SRC/booboot.ini" > "/etc/booboot/$NAME.ini"
    echo "Created /etc/booboot/$NAME.ini"
fi

# Only root may change the server, its configuration and its data, whatever the defaults
# of this system (like inherited ACLs): scripts of clients run here as another user.
mkdir -p /var/lib/booboot
for DIR in "$PREFIX" /etc/booboot /var/lib/booboot; do
    # A file system without ACLs has none to remove.
    if command -v setfacl > /dev/null; then
        setfacl -R -b -k "$DIR" 2> /dev/null || true
    fi
    chmod -R go-w "$DIR"
done

# Before version 0.5, each DUT had its own service, booboot@NAME, which now would hold the ports.
for LINK in /etc/systemd/system/multi-user.target.wants/booboot@*.service; do
    if [ -L "$LINK" ]; then
        systemctl disable "$(basename "$LINK")"
    fi
done
systemctl stop 'booboot@*.service' || true
rm -f /etc/systemd/system/booboot@.service

install -m 644 "$SRC/booboot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable booboot
systemctl restart booboot

echo "Service booboot started, with the DUTs: $(cd /etc/booboot && ls -- *.ini | grep -v '^server.ini$' | sed 's/\.ini$//' | tr '\n' ' ')"
echo "Configuration: /etc/booboot/server.ini and /etc/booboot/NAME.ini (then: systemctl restart booboot)"
echo "Logs: journalctl -u booboot"
