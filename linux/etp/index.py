"""Persistent filename index, rebuilt transactionally without reading file contents."""

import logging
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

from .search import Matcher, normalize

LOG = logging.getLogger(__name__)
UNKNOWN = 2**64 - 1
SORTS = {"NAME": "name COLLATE NOCASE", "PATH": "path COLLATE NOCASE", "SIZE": "size",
         "EXTENSION": "extension COLLATE NOCASE", "DATE_CREATED": "created",
         "DATE_MODIFIED": "modified", "ATTRIBUTES": "attributes",
         "FILE_LIST_FILENAME": "''", "DATE_RECENTLY_CHANGED": "modified"}


def safe_name(name):
    # ETP has no escaping for line breaks or Windows path separators.
    return not any(c in name for c in "\r\n\\") and not any(0xD800 <= ord(c) <= 0xDFFF for c in name)


class Index:
    def __init__(self, root, database, drive="N:"):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Index root is not a directory")
        self.database = str(database)
        self.drive = drive.upper()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.scan_lock = threading.Lock()
        self.thread = None
        self.watcher = None
        self.reader = None
        self.changes = {}
        self.changes_lock = threading.Lock()
        self.changed = threading.Event()
        self.rescan = False
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY, relative TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, path TEXT NOT NULL, full TEXT NOT NULL,
                    extension TEXT NOT NULL, is_dir INTEGER NOT NULL,
                    size INTEGER, created REAL, modified REAL, attributes INTEGER NOT NULL,
                    normalized TEXT NOT NULL, generation INTEGER NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS names USING fts5(
                    normalized, content='entries', content_rowid='id', tokenize='trigram');
                CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
                    INSERT INTO names(rowid, normalized) VALUES(new.id, new.normalized);
                END;
                CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
                    INSERT INTO names(names,rowid,normalized) VALUES('delete',old.id,old.normalized);
                END;
                CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE OF normalized ON entries
                    WHEN old.normalized != new.normalized BEGIN
                    INSERT INTO names(names,rowid,normalized) VALUES('delete',old.id,old.normalized);
                    INSERT INTO names(rowid,normalized) VALUES(new.id,new.normalized);
                END;
            """)
            identity = str(self.root) + "|" + self.drive
            previous = db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            if previous and previous[0] != identity:
                db.execute("DELETE FROM entries")
                db.execute("DELETE FROM metadata")
            db.execute("INSERT OR REPLACE INTO metadata VALUES('identity', ?)", (identity,))
            if db.execute("SELECT 1 FROM metadata WHERE key='scanned'").fetchone():
                self.ready.set()

    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def upsert(self, db, path, info, generation):
        relative = str(path.relative_to(self.root))
        is_dir = stat.S_ISDIR(info.st_mode)
        full = self.drive + "\\" + relative.replace("/", "\\")
        parent = full.rsplit("\\", 1)[0]
        if parent == self.drive:
            parent += "\\"
        attributes = (16 if is_dir else 32) | (2 if path.name.startswith(".") else 0)
        db.execute("""
            INSERT INTO entries(relative,name,path,full,extension,is_dir,size,
                                created,modified,attributes,normalized,generation)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(relative) DO UPDATE SET
                name=excluded.name,path=excluded.path,full=excluded.full,
                extension=excluded.extension,is_dir=excluded.is_dir,
                size=excluded.size,created=excluded.created,modified=excluded.modified,
                attributes=excluded.attributes,normalized=excluded.normalized,
                generation=excluded.generation
        """, (relative, path.name, parent, full,
              "" if is_dir else path.suffix.lstrip("."), int(is_dir),
              None if is_dir else info.st_size, getattr(info, "st_birthtime", None),
              info.st_mtime, attributes, normalize(full), generation))

    def scan(self, subtree=None):
        with self.scan_lock:
            start, generation, count, skipped = time.monotonic(), time.time_ns(), 0, 0
            self.root.stat()  # Do not replace the index if the root disappears.
            if not os.access(self.root, os.R_OK | os.X_OK):
                raise PermissionError(self.root)
            db = self.connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                target = Path(subtree) if subtree is not None else self.root
                pending = [target]
                while pending:
                    if self.stop.is_set():
                        db.rollback()
                        return
                    directory = pending.pop()
                    try:
                        if self.watcher is not None:
                            self.watcher.add(directory)
                        with os.scandir(directory) as children:
                            for entry in children:
                                if not safe_name(entry.name):
                                    skipped += 1
                                    continue
                                try:
                                    info = entry.stat(follow_symlinks=False)
                                except OSError:
                                    skipped += 1
                                    continue
                                is_dir = stat.S_ISDIR(info.st_mode)
                                if not (is_dir or stat.S_ISREG(info.st_mode)):
                                    continue  # No symlinks, sockets, devices, or FIFOs.
                                self.upsert(db, Path(entry.path), info, generation)
                                count += 1
                                if is_dir:
                                    pending.append(Path(entry.path))
                                if count % 100000 == 0:
                                    LOG.info("Indexing: %d entries, %.1fs", count, time.monotonic() - start)
                    except FileNotFoundError:
                        continue
                    except PermissionError:
                        # Preserve previously indexed descendants of temporarily inaccessible folders.
                        prefix = str(directory.relative_to(self.root)) + "/"
                        db.execute("UPDATE entries SET generation=? WHERE substr(relative,1,?)=?",
                                   (generation, len(prefix), prefix))
                        skipped += 1
                if target == self.root:
                    db.execute("DELETE FROM entries WHERE generation != ?", (generation,))
                    db.execute("INSERT OR REPLACE INTO metadata VALUES('scanned', ?)", (str(time.time()),))
                else:
                    prefix = str(target.relative_to(self.root)) + "/"
                    db.execute("DELETE FROM entries WHERE generation != ? AND relative>=? AND relative<?",
                               (generation, prefix, prefix[:-1] + "0"))
                db.commit()
                self.ready.set()
                LOG.info("%s: %d entries scanned, %d skipped, %.1fs",
                         "Index ready" if target == self.root else "Subtree updated", count, skipped,
                         time.monotonic() - start)
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def start(self):
        from .watch import Watcher, OVERFLOW, UNMOUNT
        self.watcher = Watcher()
        def read_events():
            try:
                while not self.stop.is_set():
                    events = self.watcher.read()
                    with self.changes_lock:
                        for path, mask in events:
                            if mask & (OVERFLOW | UNMOUNT) or len(self.changes) >= 100000:
                                self.rescan = True
                                self.changes.clear()
                            elif path is not None:
                                self.changes[path] = self.changes.get(path, 0) | mask
                        if events:
                            self.changed.set()
            except Exception:
                LOG.exception("inotify reader failed")
                # Exit and let systemd restart, rather than silently leaving a stale index.
                os._exit(1)
        self.reader = threading.Thread(target=read_events, name="etp-inotify", daemon=True)
        self.reader.start()
        def worker():
            try:
                self.scan()  # Reconcile changes made while the service was offline, once per start.
                LOG.info("inotify ready: watching %d directories; periodic scans disabled",
                         len(self.watcher.descriptors))
                while not self.stop.is_set():
                    if not self.changed.wait(0.5):
                        continue
                    if self.stop.wait(0.15):  # Coalesce bursts of related changes.
                        break
                    with self.changes_lock:
                        changes, rescan = self.changes, self.rescan
                        self.changes, self.rescan = {}, False
                        self.changed.clear()
                    if rescan:
                        LOG.warning("inotify events lost; reconciling index")
                        self.scan()
                    else:
                        self.update_changes(changes)
            except Exception:
                LOG.exception("Index watcher failed")
                os._exit(1)
        self.thread = threading.Thread(target=worker, name="etp-index", daemon=True)
        self.thread.start()

    def update_changes(self, changes):
        from .watch import ISDIR, MOVED_FROM, MOVED_TO, DELETE, DELETE_SELF, MOVE_SELF
        with self.scan_lock:
            db = self.connect()
            subtrees = []
            try:
                with db:
                    for path, mask in sorted(changes.items(), key=lambda item: len(item[0].parts)):
                        if path == self.root:
                            if mask & (DELETE_SELF | MOVE_SELF):
                                raise RuntimeError("Watched root disappeared")
                            continue
                        relative_path = path.relative_to(self.root)
                        if not all(safe_name(part) for part in relative_path.parts):
                            continue
                        relative = str(relative_path)
                        if any(parent == path or parent in path.parents for parent in subtrees):
                            continue
                        # A moved directory's old watch descriptors still refer to the old inode.
                        if ((mask & ISDIR and mask & (MOVED_FROM | MOVED_TO | DELETE)) or
                                mask & (MOVE_SELF | DELETE_SELF)):
                            self.watcher.remove_tree(path)
                        try:
                            info = path.lstat()
                        except FileNotFoundError:
                            info = None
                        if info is None or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                            prefix = relative + "/"
                            db.execute("DELETE FROM entries WHERE relative=? OR (relative>=? AND relative<?)",
                                       (relative, prefix, relative + "0"))
                            continue
                        self.upsert(db, path, info, time.time_ns())
                        if stat.S_ISDIR(info.st_mode):
                            self.watcher.add(path)
                            if mask & (MOVED_TO | 0x100):
                                subtrees.append(path)
                LOG.info("inotify updated %d changed paths", len(changes))
            finally:
                db.close()
        for subtree in subtrees:
            self.scan(subtree)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=30)
        if self.reader:
            self.reader.join(timeout=2)
        if self.watcher:
            self.watcher.close()

    def stream_query(self, state, timeout=30):
        if not self.ready.is_set():
            raise RuntimeError("Initial index is still building; retry shortly")
        deadline = time.monotonic() + timeout
        match = Matcher(state["search"], state, deadline=deadline)
        secondary = Matcher(state["filter_search"], state, "filter_", deadline=deadline)
        hint = match.fts_hint(state["search"])
        secondary_hint = secondary.fts_hint(state["filter_search"])
        if secondary_hint:
            hint = "(" + hint + ") AND (" + secondary_hint + ")" if hint else secondary_hint
        where, parameters = [], []
        if state["search"] or state["filter_search"]:
            where.append("matches(name,path,full,extension,is_dir,size,created,modified)")
        if hint:
            where.append("id IN (SELECT rowid FROM names WHERE names MATCH ?)")
            parameters.append(hint)
        if state.get("hide_empty_search_results") and not state["search"]:
            where.append("0")
        sort, direction = state["sort"].rsplit("_", 1)
        if sort not in SORTS or direction not in ("ASCENDING", "DESCENDING"):
            raise ValueError("Unknown sort type")
        order = SORTS[sort] + (" DESC" if direction == "DESCENDING" else " ASC") + ", full"
        db = self.connect()
        try:
            def predicate(*values):
                row = dict(zip(("name", "path", "full", "extension", "is_dir", "size", "created", "modified"), values))
                return int(match(row) and secondary(row))
            db.create_function("matches", 8, predicate)
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            db.execute("BEGIN")  # Count and page must come from the same snapshot.
            clause = " AND ".join(where) or "1"
            total = db.execute("SELECT count(*) FROM entries WHERE " + clause, parameters).fetchone()[0]
            cursor = db.execute("SELECT * FROM entries WHERE " + clause + " ORDER BY " + order +
                                " LIMIT ? OFFSET ?", parameters + [state["count"], state["offset"]])
            def rows():
                try:
                    yield from cursor
                except sqlite3.OperationalError as exc:
                    raise ValueError("Search failed or exceeded the time limit") from exc
                finally:
                    db.close()
            return total, rows()
        except sqlite3.OperationalError as exc:
            db.close()
            raise ValueError("Search failed or exceeded the time limit") from exc
        except BaseException:
            db.close()
            raise

    def query(self, state, timeout=30):
        total, rows = self.stream_query(state, timeout)
        return total, list(rows)
