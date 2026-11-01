"""Small inotify wrapper; no polling or filesystem reads while idle."""

import ctypes
import errno
import os
from pathlib import Path
import select
import struct
import threading

MODIFY = 0x2
ATTRIB = 0x4
CLOSE_WRITE = 0x8
MOVED_FROM = 0x40
MOVED_TO = 0x80
CREATE = 0x100
DELETE = 0x200
DELETE_SELF = 0x400
MOVE_SELF = 0x800
UNMOUNT = 0x2000
OVERFLOW = 0x4000
IGNORED = 0x8000
ISDIR = 0x40000000
MASK = MODIFY | ATTRIB | CLOSE_WRITE | MOVED_FROM | MOVED_TO | CREATE | DELETE | DELETE_SELF | MOVE_SELF | UNMOUNT


class Watcher:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.paths = {}
        self.descriptors = {}
        self.lock = threading.Lock()

    def add(self, path):
        path = Path(path)
        with self.lock:
            if path in self.descriptors:
                return
            wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), MASK | 0x01000000 | 0x02000000)
            if wd < 0:
                error = ctypes.get_errno()
                if error in (errno.ENOENT, errno.EACCES, errno.ENOTDIR):
                    return
                raise OSError(error, "Unable to watch " + str(path))
            self.paths[wd] = path
            self.descriptors[path] = wd

    def remove_tree(self, path):
        path = Path(path)
        with self.lock:
            for directory, wd in list(self.descriptors.items()):
                if directory == path or path in directory.parents:
                    self.libc.inotify_rm_watch(self.fd, wd)
                    self.descriptors.pop(directory, None)
                    self.paths.pop(wd, None)

    def read(self, timeout=0.5):
        if not select.select([self.fd], [], [], timeout)[0]:
            return []
        try:
            data = os.read(self.fd, 1024 * 1024)
        except BlockingIOError:
            return []
        events, offset = [], 0
        while offset < len(data):
            wd, mask, cookie, length = struct.unpack_from("iIII", data, offset)
            name = os.fsdecode(data[offset + 16:offset + 16 + length].split(b"\0", 1)[0])
            offset += 16 + length
            with self.lock:
                path = self.paths.get(wd)
                if mask & IGNORED and path is not None:
                    self.paths.pop(wd, None)
                    self.descriptors.pop(path, None)
            if mask & OVERFLOW:
                events.append((None, mask))
            elif path is not None and not mask & IGNORED:
                events.append((path / name if name else path, mask))
        return events

    def close(self):
        os.close(self.fd)
