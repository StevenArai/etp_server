# Linux ETP/FTP server

This branch adds a standalone Linux server to the voidtools ETP plugin repository.
The original Windows plugin source is retained in `src/`. The Linux implementation
uses Python 3.11+, SQLite FTS5, and pyftpdlib instead of the Windows Everything
plugin APIs. It implements the upstream `SITE EVERYTHING` / `EVERYTHING` protocol
so Everything can search a Linux NAS and open results through FTP or SMB.

## Install on Debian 12 / Ubuntu 24.04

```sh
git clone --branch linux https://github.com/StevenArai/etp_server.git
cd etp_server
sudo sh linux/install.sh steven /PVE/NAS 192.168.1.28
```

Replace the username, NAS directory, and listening address as appropriate.
With no address argument the installer listens on `127.0.0.1`.
The installer creates a random password in `/etc/etp-server/config.toml`, installs
the application in `/opt/etp-server`, and enables `etp-server.service` at boot.
Repeat the same command to update an installation; existing credentials are retained.
The service runs as the selected Linux user, so that user's filesystem permissions
determine which NAS directories can be indexed and which files can be downloaded.

systemd waits for the storage containing the NAS path and binds it read-only to
`/srv/etp/NAS` inside the service's mount namespace. The host mount is unchanged.
The index is stored outside the NAS in `/var/lib/etp-server/index.sqlite3`.
FTP exposes only the NAS directory, with search and download permissions.

Inspect connection settings and service status locally:

```sh
sudo cat /etc/etp-server/config.toml
systemctl status etp-server.service
sudo journalctl -u etp-server.service -f
```

The first index build can take several minutes on large trees. Searches return a
temporary `451` reply until it completes; FTP downloads are available immediately.
After startup, Linux inotify watches every indexed directory and updates only changed
paths, with a short debounce for bursts. There are no periodic full-tree scans and
no filesystem reads while idle. Newly created or moved-in subtrees are scanned once
and added to the watch set. Every service start reconciles changes made while it was
offline; inotify queue overflow also triggers a recovery scan. Scans read filenames
and metadata, never file contents. Logs show progress every 100,000 entries, a final
`Index ready` count, and `inotify ready` when recursive watching is established.
The watcher works with this local Btrfs NAS, including writes made through Samba.
Remote NFS/SMB changes made by another host may not generate local inotify events.

## Connect from Everything on Windows

1. Open **Tools → Connect to ETP server**.
2. Set host `192.168.1.28`, port `2121`, and the username/password in the config.
3. Choose FTP links, or configure the following SMB path rewrite for the existing
   `\\192.168.1.28\NAS` share.

The server reports paths beneath the virtual drive `N:\`. To open them through SMB,
exit Everything completely and add these settings to its `Everything.ini`:

```ini
etp_client_rewrite_patterns=N:\
etp_client_rewrite_substitutions=\\192.168.1.28\NAS\
```

Restart Everything and reconnect. For example, `N:\Movies\sample.mp4` then opens as
`\\192.168.1.28\NAS\Movies\sample.mp4`. Authenticate to the SMB share separately
using its existing credentials. The ETP password belongs to this service.
FTP clients may download either `/Movies/sample.mp4` or `/N:/Movies/sample.mp4`.

ETP is a plaintext FTP extension. Use it on a trusted LAN, a VPN, or a tunnel.
For FTP downloads, allow the control port `2121` and passive ports `50000–50100`.
ETP search results travel on the control connection. Configure networking to match
the intended listening address; this installer does not alter the host firewall.

## Search and protocol support

Supported searches include plain text, space-separated AND terms, `|` OR, `!`
negation, `<...>` groups, quoted phrases, `*`/`?` wildcards, and:

- `file:`, `folder:`, `ext:mp4;mkv`, `name:`, `path:`, `parent:`.
- `size:>1gb`, `size:1mb..10mb` (units use powers of 1024).
- `dm:>=2026-01-01`, `dc:2026-01-01..2026-12-31` (ISO dates, UTC).
- `regex:`, `case:`, `nocase:`, `wholeword:`, `diacritics:`.

ETP state commands support search/filter text, case, whole word, path, diacritics,
regex, prefix/suffix, punctuation/whitespace options, result columns, sorting,
offset/count, and queries. Results use upstream `200-Query results`, `RESULT_COUNT`,
`PATH`, `SIZE`, Windows FILETIME timestamps, and `FILE` / `FOLDER` records.
Large result sets spool to a temporary file instead of silently truncating results.

This is a filename search backend, with a subset of Everything's search language.
Unsupported functions return an error. Content indexing, NTFS properties, and all
Everything 1.5 search functions are outside the current implementation. Creation
dates are unknown when Python's Linux `stat` does not expose a birth timestamp;
Linux ctime is never substituted for creation time. Directory sizes are unknown.
Symlinks, special files, and names containing line breaks, backslashes, or invalid
UTF-8 are skipped. The service does not provide an HTTP UI or the separate
Everything Server indexing protocol.

## Development

```sh
sudo apt-get install python3-pyftpdlib python3-regex
PYTHONPATH=linux /usr/bin/python3 -m unittest discover -s linux/tests -v
```

For other Python 3.11+ environments, install `linux/requirements.txt` in a virtual
environment. Copy `linux/config.example.toml`, change the root/database/password,
then run:

```sh
PYTHONPATH=linux python3 -m etp.server --config /path/to/config.toml
```

The test suite covers search behavior, refresh/deletion, persistent snapshots, inotify file changes and directory moves,
upstream ETP framing, authentication, session isolation, Unicode filenames, active
and passive FTP downloads, symlink/path traversal denial, and write rejection.
