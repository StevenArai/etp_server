#!/bin/sh
# Install on Debian 12 / Ubuntu 24.04 with Python 3.11 or newer.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo sh linux/install.sh [user] [NAS path] [bind address]" >&2
    exit 1
fi

etp_user=${1:-${SUDO_USER:-steven}}
etp_root=${2:-/PVE/NAS}
etp_host=${3:-127.0.0.1}
etp_source=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
id "$etp_user" >/dev/null
test -d "$etp_root"
apt-get update
apt-get install -y python3 python3-pyftpdlib python3-regex
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'
install -d -m 0755 /opt/etp-server /srv/etp/NAS
install -d -m 0750 -o root -g "$(id -gn "$etp_user")" /etc/etp-server

# Stop an existing installation before replacing its code.
if systemctl is-active --quiet etp-server.service; then
    systemctl stop etp-server.service
fi
cp -r "$etp_source/etp" /opt/etp-server/
find /opt/etp-server -name __pycache__ -type d -exec rm -r {} +
python3 - "$etp_source" "$etp_user" "$etp_root" "$etp_host" <<'PY'
import grp
import json
import os
from pathlib import Path
import pwd
import secrets
import sys

source, user, root, host = sys.argv[1:]
root = str(Path(root).resolve(strict=True))
def unit_path(value):
    return ''.join(('%%' if char == '%' else '\\x' + format(ord(char), '02x')
                    if char.isspace() or char in '\\"' else char) for char in value)
unit = (Path(source) / 'etp-server.service').read_text()
unit = unit.replace('User=steven', 'User=' + user)
unit = unit.replace('Group=steven', 'Group=' + grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name)
unit = unit.replace('RequiresMountsFor=/PVE/NAS', 'RequiresMountsFor=' + unit_path(root))
unit = unit.replace('ConditionPathIsDirectory=/PVE/NAS', 'ConditionPathIsDirectory=' + unit_path(root))
unit = unit.replace('BindReadOnlyPaths=/PVE/NAS:/srv/etp/NAS',
                    'BindReadOnlyPaths=' + unit_path(root + ':/srv/etp/NAS'))
Path('/etc/systemd/system/etp-server.service').write_text(unit)
config = Path('/etc/etp-server/config.toml')
if not config.exists():
    text = (Path(source) / 'config.example.toml').read_text()
    text = text.replace('host = "127.0.0.1"', 'host = ' + json.dumps(host))
    text = text.replace('username = "steven"', 'username = ' + json.dumps(user))
    text = text.replace('password = "CHANGE-ME"', 'password = ' + json.dumps(secrets.token_urlsafe(18)))
    descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, 'w') as output:
        output.write(text)
    os.chown(config, 0, pwd.getpwnam(user).pw_gid)
PY
systemctl daemon-reload
systemctl enable --now etp-server.service
echo "Installed etp-server.service. Connection settings: /etc/etp-server/config.toml"
echo "Index progress: sudo journalctl -u etp-server.service -f"
