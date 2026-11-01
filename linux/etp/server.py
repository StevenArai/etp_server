"""FTP transport and upstream-compatible SITE EVERYTHING result framing."""

import argparse
import logging
import os
from pathlib import Path
import re
import signal
import tempfile
import tomllib

import regex

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.filesystems import AbstractedFS
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import ThreadedFTPServer

from .index import Index, SORTS, UNKNOWN, safe_name
from . import __version__

FLAGS = ("case", "whole_word", "path", "diacritics", "regex", "prefix", "suffix",
         "ignore_punctuation", "ignore_whitespace", "hide_empty_search_results")
COLUMNS = ("size", "date_created", "date_modified", "attributes", "path",
           "file_list_filename", "date_recently_changed")


def default_state():
    state = {key: 0 for key in FLAGS}
    state.update({"filter_" + key: 0 for key in FLAGS})
    state.update({key + "_column": 0 for key in COLUMNS})
    state.update(search="", filter_search="", sort="NAME_ASCENDING", offset=0, count=2**32 - 1)
    return state


def filetime(timestamp):
    # Linux ctime is metadata-change time, not Windows creation time.
    return UNKNOWN if timestamp is None else int((timestamp + 11644473600) * 10000000)


class QueryProducer:
    def __init__(self, file):
        self.file = file

    def more(self):
        data = self.file.read(65536)
        if not data:
            self.file.close()
        return data


class NASFilesystem(AbstractedFS):
    drive = "N:"

    def ftp2fs(self, path):
        path = path.replace("\\", "/")
        drive_path = "/" + self.drive
        if path.lower() == self.drive.lower() or path.lower().startswith(self.drive.lower() + "/"):
            path = "/" + path
        if path.lower() == drive_path.lower():
            path = "/"
        elif path.lower().startswith(drive_path.lower() + "/"):
            path = path[len(drive_path):]
        return super().ftp2fs(path)

    def listdir(self, path):
        return [name for name in super().listdir(path)
                if safe_name(name) and not os.path.islink(os.path.join(path, name))]

    def open(self, filename, mode):
        if mode != "rb":
            raise PermissionError("Read-only server")
        # Walk from an open root descriptor; reject symlinks in every component,
        # including links swapped in after pyftpdlib's initial validity check.
        relative = Path(os.path.abspath(filename)).relative_to(os.path.abspath(self.root))
        parts = relative.parts
        if not parts or any(part in (".", "..") for part in parts):
            raise PermissionError("Invalid file path")
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            import stat
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise PermissionError("Only regular files can be downloaded")
            return os.fdopen(descriptor, "rb")
        finally:
            os.close(directory)


class ETPHandler(FTPHandler):
    banner = "Everything ETP/FTP for Linux " + __version__
    encoding = "utf-8"
    unicode_errors = "replace"
    timeout = 300
    index = None
    proto_cmds = FTPHandler.proto_cmds.copy()
    for command in ("EVERYTHING", "SITE EVERYTHING"):
        proto_cmds[command] = {"perm": None, "auth": True, "arg": True,
                               "help": "Syntax: " + command + " <subcommand> [value]"}

    def __init__(self, *args, **kwargs):
        self.state = default_state()
        self.query_files = []
        super().__init__(*args, **kwargs)
        self._extra_feats.append("EVERYTHING")

    def on_login(self, username):
        self.state = default_state()

    def close(self):
        super().close()
        for file in self.query_files:
            file.close()
        self.query_files.clear()

    def ftp_HELP(self, line):
        if line.upper() == "SITE":
            self.push("214-Site commands:\r\n EVERYTHING\r\n")
            self.respond("214 End")
        else:
            super().ftp_HELP(line)

    def ftp_SITE_EVERYTHING(self, line):
        self.ftp_EVERYTHING(line)

    def ftp_EVERYTHING(self, line):
        command, _, value = line.partition(" ")
        key = command.lower()
        try:
            if key == "query":
                total, rows = self.index.stream_query(self.state)
                output = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
                def write(line):
                    output.write(line.encode("utf-8"))
                try:
                    write("200-Query results\r\n RESULT_COUNT " + str(total) + "\r\n")
                    for row in rows:
                        if self.state["path_column"]:
                            write(" PATH " + row["path"] + "\r\n")
                        for column, number in (("attributes", row["attributes"]),
                                               ("size", UNKNOWN if row["size"] is None else row["size"]),
                                               ("date_modified", filetime(row["modified"])),
                                               ("date_created", filetime(row["created"])),
                                               ("date_recently_changed", filetime(row["modified"]))):
                            if self.state[column + "_column"]:
                                write(" " + column.upper() + " " + str(number) + "\r\n")
                        if self.state["file_list_filename_column"]:
                            write(" FILE_LIST_FILENAME \r\n")
                        write(" " + ("FOLDER" if row["is_dir"] else "FILE") + " " + row["name"] + "\r\n")
                    write("200 End.\r\n")
                except BaseException:
                    rows.close()
                    output.close()
                    raise
                output.seek(0)
                self.query_files = [file for file in self.query_files if not file.closed]
                self.query_files.append(output)
                self.push_with_producer(QueryProducer(output))
                return
            if key in ("search", "filter_search"):
                self.state[key] = value
            elif key == "sort":
                sort, direction = value.upper().rsplit("_", 1)
                if sort not in SORTS or direction not in ("ASCENDING", "DESCENDING"):
                    raise ValueError("Unknown sort type")
                self.state[key] = value.upper()
            elif key in ("count", "offset"):
                number = int(value)
                if not 0 <= number <= 2**32 - 1:
                    raise ValueError("Count and offset must be unsigned 32-bit integers")
                self.state[key] = number
            elif key in self.state:
                self.state[key] = int(bool(int(value)))
            else:
                self.respond("500 Unknown Everything command.")
                return
            self.respond("200 " + command.upper() + " set.")
        except RuntimeError as exc:
            self.respond("451 " + str(exc))
        except (ValueError, TimeoutError, RecursionError, regex.error) as exc:
            self.respond("501 " + str(exc).replace("\r", " ").replace("\n", " "))


def make_server(config, index):
    authorizer = DummyAuthorizer()
    server_config = config.get("server", {})
    username = server_config.get("username", "steven")
    password = server_config.get("password", "")
    if not password:
        raise ValueError("Set a nonempty server.password in the configuration")
    authorizer.add_user(username, password, str(index.root), perm="elr")
    filesystem = type("ConfiguredFilesystem", (NASFilesystem,), {"drive": index.drive})
    handler = type("ConfiguredETPHandler", (ETPHandler,), {
        "authorizer": authorizer, "index": index, "abstracted_fs": filesystem,
        "passive_ports": range(server_config.get("passive_port_min", 50000),
                               server_config.get("passive_port_max", 50100) + 1),
    })
    server = ThreadedFTPServer((server_config.get("host", "127.0.0.1"),
                                server_config.get("port", 2121)), handler)
    server.max_cons = 32
    server.max_cons_per_ip = 8
    return server


def main():
    parser = argparse.ArgumentParser(description="Linux ETP/FTP server for Everything clients")
    parser.add_argument("--config", default="/etc/etp-server/config.toml")
    parser.add_argument("--scan-only", action="store_true", help="Build/update the index and exit")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config, "rb") as source:
        config = tomllib.load(source)
    settings = config.get("index", {})
    drive = settings.get("drive", "N:")
    if not re.fullmatch(r"[A-Za-z]:", drive):
        parser.error("index.drive must be a Windows drive letter, e.g. N:")
    index = Index(settings.get("root", "/srv/etp/NAS"),
                  settings.get("database", "/var/lib/etp-server/index.sqlite3"), drive)
    if args.scan_only:
        index.scan()
        return
    server = make_server(config, index)
    def shutdown(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    index.start()
    try:
        server.serve_forever(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.close_all()
        index.close()


if __name__ == "__main__":
    main()
