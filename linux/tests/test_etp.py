import ftplib
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from etp.index import Index, UNKNOWN
from etp.server import default_state, filetime


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "NAS"
        self.root.mkdir()
        (self.root / "电影").mkdir()
        (self.root / "电影" / "Café 电影.mp4").write_bytes(b"movie" * 300)
        (self.root / "alpha.txt").write_bytes(b"first")
        (self.root / "alphabet.txt").write_bytes(b"second")
        (self.root / "secret-link").symlink_to("/etc/passwd")
        (self.root / "unsafe\nname.txt").write_bytes(b"no")
        self.index = Index(self.root, Path(self.temp.name) / "index.sqlite3")
        self.index.scan()

    def tearDown(self):
        self.index.close()
        self.temp.cleanup()

    def query(self, search="", **options):
        state = default_state()
        state.update(search=search, **options)
        return self.index.query(state)

    def test_search_language(self):
        self.assertEqual(self.query('ext:mp4 "cafe 电影"')[0], 1)
        self.assertEqual(self.query("cafe", diacritics=1)[0], 0)
        self.assertEqual(self.query("alpha", whole_word=1)[0], 1)
        self.assertEqual(self.query("<alpha | 电影> !folder:")[0], 3)
        self.assertEqual(self.query("<alpha | caf> !folder:")[0], 3)
        self.assertEqual(self.query("alpha | absent")[0], 2)
        self.assertEqual(self.query("alpha !alphabet")[0], 1)
        self.assertEqual(self.query("size:>1kb")[0], 1)
        self.assertEqual(self.query("size:<=5")[0], 1)
        self.assertEqual(self.query("file:*.txt")[0], 2)
        self.assertEqual(self.query("file:a[l]*.txt")[0], 2)
        self.assertEqual(self.query("N:\\电影", path=1)[0], 2)
        self.assertEqual(self.query(r".*\D+\.mp4$", regex=1)[0], 1)
        self.assertEqual(self.query(r"regex:(?:alpha).*txt$")[0], 2)
        self.assertEqual(self.query("alpha", filter_search="bet")[0], 1)
        self.assertEqual(self.query("secret-link")[0], 0)
        self.assertEqual(self.query("unsafe")[0], 0)
        self.assertEqual(self.query("", hide_empty_search_results=1)[0], 0)
        with self.assertRaises(ValueError):
            self.query("content:private")

    def test_refresh_and_pagination(self):
        total, rows = self.query("", count=1, offset=1, sort="NAME_DESCENDING")
        self.assertEqual(total, 4)
        self.assertEqual(len(rows), 1)
        (self.root / "alpha.txt").unlink()
        (self.root / "new.txt").write_bytes(b"new")
        shutil.rmtree(self.root / "电影")
        self.index.scan()
        self.assertEqual(self.query("alpha.txt")[0], 0)
        self.assertEqual(self.query("new")[0], 1)
        self.assertEqual(self.query("电影")[0], 0)
        loaded = Index(self.root, self.index.database)
        self.assertTrue(loaded.ready.is_set())
        self.assertEqual(loaded.query(default_state())[0], 2)

    def test_filetime(self):
        self.assertEqual(filetime(0), 116444736000000000)
        self.assertEqual(filetime(None), UNKNOWN)

    def test_large_result_is_not_truncated(self):
        with self.index.connect() as db:
            db.executemany("""INSERT INTO entries(relative,name,path,full,extension,is_dir,
                              attributes,normalized,generation) VALUES(?,?,?,?,?,0,32,?,0)""",
                           ((f"bulk/{n}", f"file{n}", "N:\\bulk", f"N:\\bulk\\file{n}", "",
                             f"n:\\bulk\\file{n}") for n in range(10010)))
        total, rows = self.query("file")
        self.assertEqual(total, 10010)
        self.assertEqual(len(rows), 10010)

    def wait_result(self, search, count):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.query(search)[0] == count:
                return
            time.sleep(0.05)
        self.assertEqual(self.query(search)[0], count)

    def test_inotify_changes_and_directory_moves(self):
        self.index.start()
        deadline = time.monotonic() + 10
        while self.root not in self.index.watcher.descriptors and time.monotonic() < deadline:
            time.sleep(0.05)
        path = self.root / "watched-new.txt"
        path.write_bytes(b"new")
        self.wait_result("watched-new", 1)
        path.write_bytes(b"updated-content")
        self.wait_result("watched-new size:>10", 1)
        path.unlink()
        self.wait_result("watched-new", 0)
        nested = self.root / "created-dir" / "deep"
        nested.mkdir(parents=True)
        (nested / "nested-file.txt").write_bytes(b"deep")
        self.wait_result("nested-file", 1)
        (self.root / "created-dir").rename(self.root / "renamed-dir")
        self.wait_result("path:renamed-dir nested-file", 1)
        self.wait_result("path:created-dir", 0)
        (self.root / "renamed-dir" / "deep" / "after-rename.txt").write_bytes(b"after")
        self.wait_result("after-rename", 1)
        shutil.rmtree(self.root / "renamed-dir")
        self.wait_result("nested-file", 0)
        self.wait_result("after-rename", 0)
        external = Path(self.temp.name) / "external"
        external.mkdir()
        (external / "moved-in-file.txt").write_bytes(b"in")
        external.rename(self.root / "moved-in")
        self.wait_result("moved-in-file", 1)
        (self.root / "moved-in").rename(Path(self.temp.name) / "moved-out")
        self.wait_result("moved-in-file", 0)
        with self.index.connect() as db:
            last_scan = db.execute("SELECT value FROM metadata WHERE key='scanned'").fetchone()[0]
        time.sleep(1.5)
        with self.index.connect() as db:
            self.assertEqual(last_scan, db.execute("SELECT value FROM metadata WHERE key='scanned'").fetchone()[0])


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / "NAS"
        cls.root.mkdir()
        (cls.root / "电影").mkdir()
        (cls.root / "电影" / "电影 sample.txt").write_bytes("中文内容".encode())
        (cls.root / "escape").symlink_to("/etc/passwd")
        (cls.root / "escape-dir").symlink_to("/etc")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        config = Path(cls.temp.name) / "config.toml"
        config.write_text(f'''[server]
host="{'::' if socket.has_dualstack_ipv6() else '127.0.0.1'}"
port={cls.port}
username="test"
password="test-password"
[index]
root="{cls.root}"
database="{cls.temp.name}/index.sqlite3"
''')
        cls.log = open(Path(cls.temp.name) / "server.log", "w+")
        cls.process = subprocess.Popen([sys.executable, "-m", "etp.server", "--config", str(config)],
                                       stdout=cls.log, stderr=cls.log)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ftp = None
            try:
                ftp = cls.connect()
                ftp.sendcmd("EVERYTHING QUERY")
                break
            except (OSError, ftplib.Error):
                time.sleep(0.1)
            finally:
                if ftp is not None:
                    ftp.close()
        else:
            cls.log.seek(0)
            raise AssertionError(cls.log.read())

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=10)
        cls.log.close()
        cls.temp.cleanup()

    @classmethod
    def connect(cls, login=True, host="127.0.0.1"):
        ftp = ftplib.FTP()
        ftp.connect(host, cls.port, timeout=15)
        if login:
            ftp.login("test", "test-password")
        return ftp

    def test_auth_and_framing(self):
        with self.connect(False) as ftp:
            with self.assertRaises(ftplib.error_perm):
                ftp.sendcmd("SITE EVERYTHING QUERY")
            with self.assertRaises(ftplib.error_perm):
                ftp.login("test", "wrong-password")
        with self.connect() as ftp:
            self.assertIn(" EVERYTHING", ftp.sendcmd("FEAT"))
            self.assertIn(" EVERYTHING", ftp.sendcmd("HELP SITE"))
            ftp.sendcmd("SITE EVERYTHING SEARCH sample")
            ftp.sendcmd("SITE EVERYTHING PATH_COLUMN 1")
            ftp.sendcmd("EVERYTHING SIZE_COLUMN 1")
            result = ftp.sendcmd("SITE EVERYTHING QUERY")
            self.assertIn(" RESULT_COUNT 1\n", result)
            self.assertIn(" PATH N:\\电影\n", result)
            self.assertIn(" SIZE 12\n", result)
            self.assertIn(" FILE 电影 sample.txt\n", result)
            self.assertTrue(result.endswith("200 End."))
            for command in ("COUNT -1", "SORT WRONG", "UNSUPPORTED 1", "REGEX 1"):
                if command == "REGEX 1":
                    ftp.sendcmd("EVERYTHING " + command)
                    ftp.sendcmd("EVERYTHING SEARCH [")
                    command = "QUERY"
                with self.assertRaises(ftplib.error_perm):
                    ftp.sendcmd("EVERYTHING " + command)
            self.assertTrue(ftp.voidcmd("NOOP").startswith("200 "))

    @unittest.skipUnless(socket.has_dualstack_ipv6(), "IPv6 dual stack unavailable")
    def test_ipv6_query_and_download(self):
        with self.connect(host="::1") as ftp:
            ftp.sendcmd("SITE EVERYTHING SEARCH sample")
            self.assertIn("RESULT_COUNT 1", ftp.sendcmd("SITE EVERYTHING QUERY"))
            data = bytearray()
            ftp.retrbinary("RETR /N:/电影/电影 sample.txt", data.extend)
            self.assertEqual(bytes(data), "中文内容".encode())

    def test_download_permissions_and_sessions(self):
        with self.connect() as ftp, self.connect() as second:
            ftp.sendcmd("EVERYTHING SEARCH sample")
            second.sendcmd("EVERYTHING SEARCH absent")
            self.assertIn("RESULT_COUNT 1", ftp.sendcmd("EVERYTHING QUERY"))
            self.assertIn("RESULT_COUNT 0", second.sendcmd("EVERYTHING QUERY"))
            for passive in (True, False):
                ftp.set_pasv(passive)
                data = bytearray()
                ftp.retrbinary("RETR /N:/电影/电影 sample.txt", data.extend)
                self.assertEqual(bytes(data), "中文内容".encode())
            data = bytearray()
            ftp.retrbinary("RETR N:\\电影\\电影 sample.txt", data.extend)
            self.assertEqual(bytes(data), "中文内容".encode())
            for path in ("/escape", "/escape-dir/passwd", "/../../etc/passwd"):
                with self.assertRaises(ftplib.error_perm):
                    ftp.retrbinary("RETR " + path, lambda data: None)
            with self.assertRaises(ftplib.error_perm):
                ftp.storbinary("STOR changed.txt", io.BytesIO(b"no"))
            with self.assertRaises(ftplib.error_perm):
                ftp.delete("/电影/电影 sample.txt")
            self.assertFalse((self.root / "changed.txt").exists())


if __name__ == "__main__":
    unittest.main()
