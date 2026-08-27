"""Tests for mdlive.

Run with:  python3 -m unittest discover -s tests -v

The HTTP tests start a real server on an ephemeral port and talk to it over
the loopback interface. That is slower than calling the handler methods
directly, but it exercises the thing that actually ships: routing, status
codes, and headers included.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mdlive  # noqa: E402


class ServerTestCase(unittest.TestCase):
    """Base class that serves a small temporary tree for the duration of a test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # macOS puts temp dirs under /var, a symlink to /private/var. Resolving
        # matters: the server resolves paths too, and the two must agree.
        self.root = Path(self._tmp.name).resolve()

        (self.root / "top.md").write_text("# Top\n", encoding="utf-8")
        (self.root / "notes.txt").write_text("not markdown\n", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "nested.md").write_text("# Nested\n", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "ignored.md").write_text("# Ignored\n", encoding="utf-8")

        self.server = mdlive.PreviewServer(("127.0.0.1", 0), self.root, "top.md")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    # --- helpers ----------------------------------------------------------

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str):
        """GET a path, returning (status, body_bytes, headers)."""
        try:
            with urllib.request.urlopen(self.url(path), timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def get_json(self, path: str):
        status, body, headers = self.get(path)
        return status, json.loads(body.decode("utf-8")), headers


class TestFileAPI(ServerTestCase):
    def test_serves_file_contents_and_mtime(self) -> None:
        status, payload, _ = self.get_json("/api/file?path=top.md")
        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], "# Top\n")
        self.assertEqual(payload["path"], "top.md")
        self.assertIsInstance(payload["mtime"], float)

    def test_mtime_endpoint_omits_text(self) -> None:
        _, payload, _ = self.get_json("/api/mtime?path=top.md")
        self.assertNotIn("text", payload)
        self.assertIn("mtime", payload)

    def test_mtime_changes_after_write(self) -> None:
        _, before, _ = self.get_json("/api/mtime?path=top.md")
        # A filesystem timestamp has finite resolution; sleep past it so the
        # change is guaranteed observable rather than merely likely.
        time.sleep(0.02)
        (self.root / "top.md").write_text("# Changed\n", encoding="utf-8")
        _, after, _ = self.get_json("/api/mtime?path=top.md")
        self.assertNotEqual(before["mtime"], after["mtime"])

    def test_nested_file_is_reachable(self) -> None:
        status, payload, _ = self.get_json("/api/file?path=sub/nested.md")
        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], "# Nested\n")

    def test_missing_file_is_404(self) -> None:
        status, payload, _ = self.get_json("/api/file?path=ghost.md")
        self.assertEqual(status, 404)
        self.assertIn("no longer exists", payload["error"])

    def test_unreadable_bytes_do_not_fail_the_request(self) -> None:
        # A writer caught mid-flush can leave invalid UTF-8 on disk. Serving
        # replacement characters beats returning an error to the poller.
        (self.root / "top.md").write_bytes(b"# Broken \xff\xfe\n")
        status, payload, _ = self.get_json("/api/file?path=top.md")
        self.assertEqual(status, 200)
        self.assertIn("�", payload["text"])


class TestPathSafety(ServerTestCase):
    def test_rejects_parent_traversal(self) -> None:
        status, payload, _ = self.get_json("/api/file?path=../../../../etc/hosts")
        self.assertEqual(status, 400)
        self.assertIn("escapes", payload["error"])

    def test_rejects_percent_encoded_traversal(self) -> None:
        status, payload, _ = self.get_json("/api/file?path=%2e%2e%2f%2e%2e%2fsecret.md")
        self.assertEqual(status, 400)
        self.assertIn("escapes", payload["error"])

    def test_rejects_symlink_pointing_outside_root(self) -> None:
        # _resolve() calls Path.resolve(), which follows symlinks, so a link
        # planted inside the root cannot be used to read outside it.
        outside = Path(self._tmp.name).parent / "mdlive-outside-target.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        os.symlink(outside, self.root / "escape.md")

        status, payload, _ = self.get_json("/api/file?path=escape.md")
        self.assertEqual(status, 400)
        self.assertIn("escapes", payload["error"])

    def test_rejects_non_markdown(self) -> None:
        status, payload, _ = self.get_json("/api/file?path=notes.txt")
        self.assertEqual(status, 400)
        self.assertIn("not a Markdown file", payload["error"])

    def test_rejects_missing_path_parameter(self) -> None:
        status, payload, _ = self.get_json("/api/file")
        self.assertEqual(status, 400)
        self.assertIn("missing", payload["error"])


class TestListing(ServerTestCase):
    def test_lists_markdown_recursively(self) -> None:
        _, payload, _ = self.get_json("/api/list")
        self.assertIn("top.md", payload["files"])
        self.assertIn("sub/nested.md", payload["files"])

    def test_excludes_non_markdown_and_ignored_dirs(self) -> None:
        _, payload, _ = self.get_json("/api/list")
        self.assertNotIn("notes.txt", payload["files"])
        self.assertNotIn("node_modules/ignored.md", payload["files"])

    def test_reports_truncation_flag(self) -> None:
        _, payload, _ = self.get_json("/api/list")
        self.assertFalse(payload["truncated"])


class TestShellAndAssets(ServerTestCase):
    def test_shell_injects_initial_file(self) -> None:
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn('var INITIAL = "top.md"', body.decode("utf-8"))

    def test_root_endpoint_reports_served_root(self) -> None:
        _, payload, _ = self.get_json("/api/root")
        self.assertEqual(payload["root"], str(self.root))

    def test_every_response_forbids_caching(self) -> None:
        # A cached response would show stale content, which is the exact bug
        # this tool exists to prevent.
        for path in ("/", "/api/list", "/api/file?path=top.md", "/vendor/marked.min.js"):
            with self.subTest(path=path):
                _, _, headers = self.get(path)
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_missing_asset_returns_comment_not_error(self) -> None:
        # A 404 here would log a console error in the browser before the CDN
        # fallback runs, so the server answers 200 with a JS comment.
        status, body, headers = self.get("/vendor/marked.min.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertTrue(body.decode("utf-8").startswith("//"))

    def test_unknown_asset_is_404(self) -> None:
        status, payload, _ = self.get_json("/vendor/evil.js")
        self.assertEqual(status, 404)
        self.assertIn("unknown asset", payload["error"])

    def test_unknown_route_is_404(self) -> None:
        status, payload, _ = self.get_json("/nope")
        self.assertEqual(status, 404)


class TestInstanceRegistry(unittest.TestCase):
    """The registry is what lets a second `mdlive foo.md` reuse a live server."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "doc.md").write_text("# Doc\n", encoding="utf-8")
        (self.root / "deep").mkdir()
        (self.root / "deep" / "doc.md").write_text("# Deep\n", encoding="utf-8")

        # Point the registry at a temp location so a real running mdlive on
        # this machine cannot influence the test, and vice versa.
        self._real_registry = mdlive.REGISTRY_DIR
        mdlive.REGISTRY_DIR = self.root / "_registry"

        self.server = mdlive.PreviewServer(("127.0.0.1", 0), self.root, None)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        mdlive.REGISTRY_DIR = self._real_registry
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def test_probe_returns_root_of_live_server(self) -> None:
        self.assertEqual(mdlive.probe_instance(self.port), self.root)

    def test_probe_returns_none_for_dead_port(self) -> None:
        dead = mdlive.PreviewServer(("127.0.0.1", 0), self.root, None)
        port = dead.server_address[1]
        dead.server_close()
        self.assertIsNone(mdlive.probe_instance(port, timeout=0.2))

    def test_finds_instance_serving_the_files_directory(self) -> None:
        mdlive.register_instance(self.port, self.root)
        found = mdlive.find_reusable_instance(self.root / "doc.md")
        self.assertEqual(found, (self.port, self.root))

    def test_finds_instance_serving_an_ancestor_directory(self) -> None:
        mdlive.register_instance(self.port, self.root)
        found = mdlive.find_reusable_instance(self.root / "deep" / "doc.md")
        self.assertIsNotNone(found)
        self.assertEqual(found[0], self.port)

    def test_ignores_instance_not_covering_the_file(self) -> None:
        mdlive.register_instance(self.port, self.root)
        self.assertIsNone(mdlive.find_reusable_instance(Path("/etc/hosts.md")))

    def test_prunes_stale_registry_entries(self) -> None:
        dead = mdlive.PreviewServer(("127.0.0.1", 0), self.root, None)
        dead_port = dead.server_address[1]
        dead.server_close()
        mdlive.register_instance(dead_port, self.root)

        self.assertTrue(mdlive.registry_entry(dead_port).exists())
        mdlive.find_reusable_instance(self.root / "doc.md")
        self.assertFalse(mdlive.registry_entry(dead_port).exists())

    def test_unregister_removes_the_entry(self) -> None:
        mdlive.register_instance(self.port, self.root)
        mdlive.unregister_instance(self.port)
        self.assertFalse(mdlive.registry_entry(self.port).exists())

    def test_unregister_is_idempotent(self) -> None:
        mdlive.unregister_instance(59999)  # never registered; must not raise


class TestCLI(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "doc.md").write_text("# Doc\n", encoding="utf-8")
        (self.root / "plain.txt").write_text("x\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rejects_non_markdown_target(self) -> None:
        with self.assertRaises(SystemExit):
            mdlive.main([str(self.root / "plain.txt"), "--no-open"])

    def test_rejects_missing_target(self) -> None:
        with self.assertRaises(SystemExit):
            mdlive.main([str(self.root / "absent.md"), "--no-open"])

    def test_start_server_scans_past_a_busy_port(self) -> None:
        blocker = mdlive.PreviewServer(("127.0.0.1", 0), self.root, None)
        busy_port = blocker.server_address[1]
        try:
            server = mdlive.start_server("127.0.0.1", busy_port, self.root, None)
            try:
                self.assertNotEqual(server.server_address[1], busy_port)
                self.assertGreater(server.server_address[1], busy_port)
            finally:
                server.server_close()
        finally:
            blocker.server_close()


if __name__ == "__main__":
    unittest.main()
