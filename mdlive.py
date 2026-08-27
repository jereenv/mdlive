#!/usr/bin/env python3
"""mdlive - a live-reloading Markdown viewer for macOS.

Serves Markdown files as GitHub-styled HTML in the browser and re-renders the
page automatically whenever a file changes on disk. An external tool rewriting
the file (an editor, a code-generation agent) never leaves you looking at a
stale render, and you never have to reopen anything.

Usage:
    mdlive                          # serve the current directory
    mdlive notes.md                 # open notes.md
    mdlive ~/personal --port 9000   # serve a whole tree on a chosen port

Running mdlive on a file already covered by a running instance reuses that
instance instead of starting a second server, which is what makes it safe to
wire up as the system handler for .md files.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import sys
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

__version__ = "1.1.0"

MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdown", ".mkd"})

# Directories that never hold Markdown worth previewing but do hold enough
# files to make the sidebar listing slow.
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".next",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)

# Upper bound on the sidebar listing, so pointing mdlive at a huge tree stays
# responsive. Reaching it is reported to the client rather than hidden.
MAX_LISTED_FILES = 500

# Assets are served from next to this script when vendored, so the viewer keeps
# working with no network. Absent that, the page falls back to a CDN.
ASSET_DIR = Path(__file__).resolve().parent / "vendor"
ASSETS = {
    "marked.min.js": "application/javascript; charset=utf-8",
    "highlight.min.js": "application/javascript; charset=utf-8",
}

# Where running instances announce themselves so a second invocation can find
# and reuse them. Caches is correct for this: losing it costs nothing.
REGISTRY_DIR = Path.home() / "Library" / "Caches" / "mdlive" / "servers"

DEFAULT_PORT = 8765
PORT_SCAN_ATTEMPTS = 20


# --------------------------------------------------------------------------
# Instance registry
# --------------------------------------------------------------------------


def registry_entry(port: int) -> Path:
    return REGISTRY_DIR / f"{port}.json"


def register_instance(port: int, root: Path) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"root": str(root), "pid": os.getpid(), "version": __version__}
    registry_entry(port).write_text(json.dumps(payload), encoding="utf-8")


def unregister_instance(port: int) -> None:
    try:
        registry_entry(port).unlink()
    except OSError:
        pass


def probe_instance(port: int, timeout: float = 0.4) -> Optional[Path]:
    """Return the root served by a live mdlive on `port`, or None.

    A registry file only records that an instance once existed. This asks the
    process itself, which is the only way to know it is still alive and still
    serving what the file claims. Any failure means "not usable", so the broad
    except is deliberate: URLError, timeouts, and malformed JSON are all just
    "no".
    """
    url = f"http://127.0.0.1:{port}/api/root"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return Path(data["root"])
    except Exception:
        return None


def find_reusable_instance(target: Path) -> Optional[Tuple[int, Path]]:
    """Find a running instance whose served root contains `target`.

    Prunes registry entries whose process is gone, so a crashed instance does
    not leave a stale file behind forever.
    """
    if not REGISTRY_DIR.is_dir():
        return None
    for entry in sorted(REGISTRY_DIR.glob("*.json")):
        try:
            port = int(entry.stem)
        except ValueError:
            continue
        root = probe_instance(port)
        if root is None:
            entry.unlink(missing_ok=True)
            continue
        if root in target.parents:
            return port, root
    return None


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


class PreviewServer(ThreadingHTTPServer):
    """HTTP server carrying the served root and the initially opened file."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], root: Path, initial: Optional[str]) -> None:
        # Set attributes before super().__init__, which binds the socket and
        # may begin accepting connections immediately.
        self.root = root
        self.initial = initial
        super().__init__(address, PreviewHandler)


class PreviewHandler(BaseHTTPRequestHandler):
    """Routes the page shell, the static assets, and the small JSON file API."""

    server_version = f"mdlive/{__version__}"
    protocol_version = "HTTP/1.1"

    server: PreviewServer  # narrowed for readers; assigned by the base class

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self._send_shell()
            elif parsed.path == "/api/root":
                self._send_json(HTTPStatus.OK, {"root": str(self.server.root)})
            elif parsed.path == "/api/list":
                self._send_listing()
            elif parsed.path == "/api/file":
                self._send_file_payload(query, include_text=True)
            elif parsed.path == "/api/mtime":
                self._send_file_payload(query, include_text=False)
            elif parsed.path.startswith("/vendor/"):
                self._send_asset(parsed.path[len("/vendor/") :])
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "no such route"})
        except (BrokenPipeError, ConnectionResetError):
            # The browser navigated away or reloaded mid-response. Not an error.
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence the per-request access log.

        The browser polls a few times a second, which would otherwise bury the
        startup banner in noise. Real failures still surface in the browser.
        """

    # --- handlers ---------------------------------------------------------

    def _send_shell(self) -> None:
        page = PAGE_TEMPLATE.replace("__INITIAL__", json.dumps(self.server.initial))
        self._send_bytes(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")

    def _send_listing(self) -> None:
        files = self._list_markdown()
        self._send_json(
            HTTPStatus.OK,
            {"files": files, "truncated": len(files) >= MAX_LISTED_FILES},
        )

    def _send_file_payload(self, query: Dict[str, List[str]], include_text: bool) -> None:
        relative = (query.get("path") or [""])[0]
        try:
            path = self._resolve(unquote(relative))
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        try:
            stat = path.stat()
        except OSError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "file no longer exists"})
            return

        payload: Dict[str, Any] = {
            "path": relative,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
        }
        if include_text:
            # errors="replace" keeps a half-written file from failing the poll:
            # a writer may be mid-flush when we read it.
            payload["text"] = path.read_text(encoding="utf-8", errors="replace")
        self._send_json(HTTPStatus.OK, payload)

    def _send_asset(self, name: str) -> None:
        content_type = ASSETS.get(name)
        if content_type is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown asset"})
            return
        path = ASSET_DIR / name
        if path.is_file():
            body = path.read_bytes()
        else:
            # A 200 carrying a comment rather than a 404, so the page's script
            # tag does not log a console error before the CDN fallback runs.
            body = f"// mdlive: {name} not vendored; falling back to CDN\n".encode("utf-8")
        self._send_bytes(HTTPStatus.OK, body, content_type)

    # --- helpers ----------------------------------------------------------

    def _resolve(self, relative: str) -> Path:
        """Map a client-supplied relative path to a real file under the root.

        Rejects anything escaping the root, such as '../../.ssh/id_rsa'. A path
        arriving over the network is untrusted input even from localhost: this
        check is what makes the server safe to leave running.
        """
        if not relative:
            raise ValueError("missing 'path' parameter")

        root = self.server.root
        candidate = (root / relative).resolve()
        if root != candidate and root not in candidate.parents:
            raise ValueError("path escapes the served root")
        if candidate.suffix.lower() not in MARKDOWN_SUFFIXES:
            raise ValueError("not a Markdown file")
        return candidate

    def _list_markdown(self) -> List[str]:
        root = self.server.root
        found: List[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            # Assigning into the slice prunes the walk in place, so os.walk
            # never descends into the skipped directories at all.
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            for name in sorted(filenames):
                if Path(name).suffix.lower() not in MARKDOWN_SUFFIXES:
                    continue
                found.append(str((Path(dirpath) / name).relative_to(root)))
                if len(found) >= MAX_LISTED_FILES:
                    return found
        return found

    def _send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This tool's whole value is freshness, so every route opts out of the
        # browser cache explicitly. Without this the viewer would happily show
        # a cached copy - exactly the bug it exists to fix.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mdlive</title>
<style>
  /* Palette lifted from GitHub's Primer light/dark themes so a document reads
     the same here as it does on github.com. */
  :root {
    color-scheme: light dark;
    --canvas: #ffffff;
    --canvas-subtle: #f6f8fa;
    --canvas-inset: #f6f8fa;
    --fg: #1f2328;
    --fg-muted: #59636e;
    --border: #d1d9e0;
    --border-muted: #d1d9e0b3;
    --accent: #0969da;
    --neutral-emphasis: #59636e;
    --success: #1a7f37;
    --attention: #9a6700;
    --code-bg: #818b981f;
    --hl-comment: #59636e;
    --hl-keyword: #cf222e;
    --hl-string: #0a3069;
    --hl-title: #6639ba;
    --hl-number: #0550ae;
    --hl-variable: #953800;
    --hl-tag: #0550ae;
    --hl-section: #0550ae;
    --hl-addition-bg: #dafbe1;
    --hl-deletion-bg: #ffebe9;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --canvas: #0d1117;
      --canvas-subtle: #151b23;
      --canvas-inset: #010409;
      --fg: #e6edf3;
      --fg-muted: #9198a1;
      --border: #3d444d;
      --border-muted: #3d444db3;
      --accent: #4493f8;
      --neutral-emphasis: #6e7681;
      --success: #3fb950;
      --attention: #d29922;
      --code-bg: #656c7633;
      --hl-comment: #9198a1;
      --hl-keyword: #ff7b72;
      --hl-string: #a5d6ff;
      --hl-title: #d2a8ff;
      --hl-number: #79c0ff;
      --hl-variable: #ffa657;
      --hl-tag: #7ee787;
      --hl-section: #1f6feb;
      --hl-addition-bg: #12261e;
      --hl-deletion-bg: #25171c;
    }
  }

  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0;
    background: var(--canvas);
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans",
                 Helvetica, Arial, sans-serif, "Apple Color Emoji";
    font-size: 16px;
    line-height: 1.5;
    display: flex;
    min-height: 100vh;
  }

  /* --- sidebar ------------------------------------------------------- */
  #sidebar {
    width: 264px;
    flex: 0 0 264px;
    border-right: 1px solid var(--border);
    background: var(--canvas-subtle);
    padding: 16px 0 24px;
    overflow-y: auto;
    max-height: 100vh;
    position: sticky;
    top: 0;
  }
  #sidebar h2 {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: .02em;
    color: var(--fg-muted);
    margin: 0 16px 8px;
  }
  #filter {
    width: calc(100% - 32px);
    margin: 0 16px 12px;
    padding: 5px 10px;
    font-size: 13px;
    color: var(--fg);
    background: var(--canvas);
    border: 1px solid var(--border);
    border-radius: 6px;
  }
  #filter:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  #files { list-style: none; margin: 0; padding: 0; }
  #files a {
    display: block;
    padding: 5px 16px;
    color: var(--fg);
    text-decoration: none;
    font-size: 13px;
    word-break: break-all;
    border-left: 2px solid transparent;
  }
  #files a:hover { background: var(--canvas); }
  #files a.active {
    border-left-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
  }

  /* --- top bar ------------------------------------------------------- */
  #main { flex: 1 1 auto; min-width: 0; }
  #bar {
    position: sticky;
    top: 0;
    z-index: 3;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 32px;
    border-bottom: 1px solid var(--border);
    background: var(--canvas);
    font-size: 12px;
    color: var(--fg-muted);
  }
  #path { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace; }
  #status { margin-left: auto; display: flex; align-items: center; gap: 6px; }
  #dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--success);
    transition: background .2s, transform .2s;
  }
  #dot.pulse { transform: scale(1.9); }
  #dot.stale { background: var(--attention); }

  /* --- document ------------------------------------------------------ */
  #doc { max-width: 900px; padding: 32px; }
  #doc > :first-child { margin-top: 0 !important; }
  #doc > :last-child { margin-bottom: 0 !important; }

  #doc h1, #doc h2, #doc h3, #doc h4, #doc h5, #doc h6 {
    margin: 24px 0 16px;
    font-weight: 600;
    line-height: 1.25;
  }
  #doc h1 { font-size: 2em; padding-bottom: .3em; border-bottom: 1px solid var(--border-muted); }
  #doc h2 { font-size: 1.5em; padding-bottom: .3em; border-bottom: 1px solid var(--border-muted); }
  #doc h3 { font-size: 1.25em; }
  #doc h4 { font-size: 1em; }
  #doc h5 { font-size: .875em; }
  #doc h6 { font-size: .85em; color: var(--fg-muted); }

  #doc p, #doc ul, #doc ol, #doc dl, #doc table, #doc pre, #doc blockquote, #doc details {
    margin: 0 0 16px;
  }
  #doc ul, #doc ol { padding-left: 2em; }
  #doc li + li { margin-top: .25em; }
  #doc li > ul, #doc li > ol { margin: .25em 0 0; }

  #doc a { color: var(--accent); text-decoration: none; }
  #doc a:hover { text-decoration: underline; }

  /* Heading anchors, revealed on hover exactly like GitHub's. */
  #doc .anchor {
    float: left;
    margin-left: -20px;
    padding-right: 4px;
    color: var(--fg-muted);
    opacity: 0;
    text-decoration: none;
    font-weight: 400;
  }
  #doc h1:hover .anchor, #doc h2:hover .anchor, #doc h3:hover .anchor,
  #doc h4:hover .anchor, #doc h5:hover .anchor, #doc h6:hover .anchor { opacity: 1; }
  #doc .anchor:hover { color: var(--accent); text-decoration: none; }

  #doc code, #doc tt {
    padding: .2em .4em;
    margin: 0;
    font-size: 85%;
    background: var(--code-bg);
    border-radius: 6px;
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }
  #doc pre {
    position: relative;
    padding: 16px;
    overflow: auto;
    font-size: 85%;
    line-height: 1.45;
    background: var(--canvas-subtle);
    border-radius: 6px;
  }
  #doc pre code {
    padding: 0;
    margin: 0;
    font-size: 100%;
    background: transparent;
    border: 0;
    white-space: pre;
    word-break: normal;
  }

  /* Copy button on code blocks. */
  .copy {
    position: absolute;
    top: 8px;
    right: 8px;
    padding: 4px 8px;
    font: 600 11px/1.4 -apple-system, BlinkMacSystemFont, sans-serif;
    color: var(--fg-muted);
    background: var(--canvas);
    border: 1px solid var(--border);
    border-radius: 6px;
    cursor: pointer;
    opacity: 0;
    transition: opacity .12s;
  }
  #doc pre:hover .copy, .copy:focus { opacity: 1; }
  .copy:hover { color: var(--fg); }
  .copy.done { color: var(--success); border-color: var(--success); }

  #doc blockquote {
    padding: 0 1em;
    color: var(--fg-muted);
    border-left: .25em solid var(--border);
  }
  #doc blockquote > :last-child { margin-bottom: 0; }

  #doc table {
    display: block;
    width: max-content;
    max-width: 100%;
    overflow: auto;
    border-collapse: collapse;
    border-spacing: 0;
  }
  #doc th, #doc td { padding: 6px 13px; border: 1px solid var(--border); }
  #doc th { font-weight: 600; background: var(--canvas-subtle); }
  #doc tr:nth-child(2n) td { background: var(--canvas-subtle); }

  #doc hr {
    height: .25em;
    padding: 0;
    margin: 24px 0;
    background: var(--border);
    border: 0;
  }
  #doc img { max-width: 100%; background: var(--canvas); }
  #doc kbd {
    display: inline-block;
    padding: 3px 5px;
    font: 11px ui-monospace, SFMono-Regular, Menlo, monospace;
    line-height: 10px;
    color: var(--fg);
    vertical-align: middle;
    background: var(--canvas-subtle);
    border: 1px solid var(--border);
    border-radius: 6px;
    box-shadow: inset 0 -1px 0 var(--border);
  }

  /* GitHub task lists: no bullet, checkbox pulled into the margin. */
  #doc .task-list-item { list-style-type: none; }
  #doc .task-list-item input[type="checkbox"] {
    margin: 0 .2em .25em -1.4em;
    vertical-align: middle;
  }
  #doc .contains-task-list { padding-left: 2em; }

  /* --- syntax highlighting (highlight.js class names) ---------------- */
  .hljs-comment, .hljs-quote { color: var(--hl-comment); font-style: italic; }
  .hljs-keyword, .hljs-selector-tag, .hljs-literal, .hljs-doctag,
  .hljs-formula, .hljs-subst { color: var(--hl-keyword); }
  .hljs-string, .hljs-regexp, .hljs-addition, .hljs-attribute,
  .hljs-meta .hljs-string { color: var(--hl-string); }
  .hljs-title, .hljs-title.class_, .hljs-title.function_,
  .hljs-section, .hljs-name { color: var(--hl-title); font-weight: 600; }
  .hljs-number, .hljs-symbol, .hljs-bullet, .hljs-link,
  .hljs-meta, .hljs-selector-id, .hljs-selector-class { color: var(--hl-number); }
  .hljs-variable, .hljs-template-variable, .hljs-attr,
  .hljs-params, .hljs-built_in, .hljs-builtin-name { color: var(--hl-variable); }
  .hljs-type, .hljs-class .hljs-title, .hljs-tag { color: var(--hl-tag); }
  .hljs-deletion { background: var(--hl-deletion-bg); }
  .hljs-addition { background: var(--hl-addition-bg); }
  .hljs-emphasis { font-style: italic; }
  .hljs-strong { font-weight: 700; }

  /* --- notices ------------------------------------------------------- */
  .notice {
    margin: 32px;
    padding: 16px 20px;
    background: var(--canvas-subtle);
    border: 1px solid var(--border);
    border-left: 3px solid var(--attention);
    border-radius: 6px;
    color: var(--fg-muted);
    font-size: 14px;
  }

  @media (max-width: 800px) {
    body { flex-direction: column; }
    #sidebar {
      width: 100%; flex: none; position: static;
      max-height: 180px; border-right: 0; border-bottom: 1px solid var(--border);
    }
    #bar, #doc { padding-left: 16px; padding-right: 16px; }
  }
</style>
<script src="/vendor/marked.min.js"></script>
<script src="/vendor/highlight.min.js"></script>
</head>
<body>
  <nav id="sidebar">
    <h2>Markdown files</h2>
    <input id="filter" type="search" placeholder="Filter files" autocomplete="off" spellcheck="false">
    <ul id="files"></ul>
  </nav>
  <div id="main">
    <div id="bar">
      <span id="path">no file selected</span>
      <span id="status"><span id="dot"></span><span id="label">live</span></span>
    </div>
    <article id="doc"></article>
  </div>

<script>
(function () {
  "use strict";

  var INITIAL = __INITIAL__;
  var POLL_MS = 400;
  // The file list walks the tree, so it is refreshed far less often than a
  // single file's timestamp is checked.
  var LIST_REFRESH_MS = 5000;
  var CDN = {
    marked: "https://cdn.jsdelivr.net/npm/marked@12/marked.min.js",
    hljs: "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"
  };

  var docEl = document.getElementById("doc");
  var pathEl = document.getElementById("path");
  var dotEl = document.getElementById("dot");
  var labelEl = document.getElementById("label");
  var filesEl = document.getElementById("files");
  var filterEl = document.getElementById("filter");

  var current = null;    // relative path currently displayed
  var lastMtime = null;  // modification time of what is on screen
  var allFiles = [];

  // --- asset loading -----------------------------------------------------

  // Each library is tried from the local /vendor mount first (see ASSET_DIR in
  // mdlive.py) and only then from a CDN, so a vendored checkout works offline.
  function loadScript(url) {
    return new Promise(function (resolve) {
      var s = document.createElement("script");
      s.src = url;
      s.onload = function () { resolve(true); };
      s.onerror = function () { resolve(false); };
      document.head.appendChild(s);
    });
  }

  function ensure(globalName, url) {
    if (window[globalName]) return Promise.resolve(true);
    return loadScript(url).then(function () { return Boolean(window[globalName]); });
  }

  var haveMarked = false;
  var haveHljs = false;

  // --- rendering ---------------------------------------------------------

  function escapeHtml(text) {
    return text.replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function toHtml(text) {
    if (!haveMarked) {
      // No renderer reachable (offline with nothing vendored): show the source
      // rather than a blank page.
      return "<pre><code>" + escapeHtml(text) + "</code></pre>";
    }
    return window.marked.parse(text, { gfm: true, breaks: false });
  }

  // GitHub-style slug: lowercase, drop punctuation, spaces to hyphens.
  function slugify(text) {
    return text.toLowerCase().trim()
      .replace(/[^\w\- ]+/g, "")
      .replace(/\s+/g, "-");
  }

  function addHeadingAnchors(root) {
    var seen = {};
    var headings = root.querySelectorAll("h1, h2, h3, h4, h5, h6");
    for (var i = 0; i < headings.length; i++) {
      var h = headings[i];
      var base = slugify(h.textContent) || "section";
      // Duplicate headings get -1, -2 suffixes, matching GitHub.
      var slug = base;
      if (seen[base] === undefined) { seen[base] = 0; } else { slug = base + "-" + (++seen[base]); }
      h.id = slug;
      var a = document.createElement("a");
      a.className = "anchor";
      a.href = "#" + slug;
      a.setAttribute("aria-label", "Permalink");
      a.textContent = "#";
      h.insertBefore(a, h.firstChild);
    }
  }

  function markTaskLists(root) {
    var boxes = root.querySelectorAll("li > input[type='checkbox']");
    for (var i = 0; i < boxes.length; i++) {
      var li = boxes[i].parentNode;
      li.classList.add("task-list-item");
      if (li.parentNode) li.parentNode.classList.add("contains-task-list");
    }
  }

  function highlight(root) {
    if (!haveHljs) return;
    var blocks = root.querySelectorAll("pre > code");
    for (var i = 0; i < blocks.length; i++) {
      var block = blocks[i];
      var match = /language-([\w+#-]+)/.exec(block.className || "");
      // Only highlight fences that declared a language, and only if
      // highlight.js actually knows it. Auto-detection guesses wrong often
      // enough to be worse than plain text, and GitHub does not do it either.
      if (!match) continue;
      if (!window.hljs.getLanguage(match[1])) continue;
      window.hljs.highlightElement(block);
    }
  }

  function addCopyButtons(root) {
    var pres = root.querySelectorAll("pre");
    for (var i = 0; i < pres.length; i++) {
      addCopyButton(pres[i]);
    }
  }

  function addCopyButton(pre) {
    var button = document.createElement("button");
    button.className = "copy";
    button.type = "button";
    button.textContent = "Copy";
    button.addEventListener("click", function () {
      var code = pre.querySelector("code");
      var text = code ? code.textContent : pre.textContent;
      // navigator.clipboard needs a secure context; http://127.0.0.1 counts as
      // one in Chrome and Safari, so no fallback is needed here.
      navigator.clipboard.writeText(text).then(function () {
        button.textContent = "Copied";
        button.classList.add("done");
        setTimeout(function () {
          button.textContent = "Copy";
          button.classList.remove("done");
        }, 1200);
      });
    });
    pre.appendChild(button);
  }

  function render(payload) {
    // Re-rendering resets scroll to the top, which is jarring mid-document.
    // Capture the position as a fraction of scrollable height so it survives
    // content that grew or shrank.
    var scrollable = document.documentElement.scrollHeight - window.innerHeight;
    var ratio = scrollable > 0 ? window.scrollY / scrollable : 0;
    var atTop = window.scrollY < 40;

    docEl.innerHTML = toHtml(payload.text);
    addHeadingAnchors(docEl);
    markTaskLists(docEl);
    highlight(docEl);
    addCopyButtons(docEl);
    lastMtime = payload.mtime;

    if (!atTop) {
      var next = document.documentElement.scrollHeight - window.innerHeight;
      window.scrollTo(0, Math.round(ratio * Math.max(next, 0)));
    }
  }

  // --- status indicator --------------------------------------------------

  function setStatus(state, text) {
    dotEl.classList.toggle("stale", state !== "live");
    labelEl.textContent = text;
  }

  function pulse() {
    dotEl.classList.add("pulse");
    setTimeout(function () { dotEl.classList.remove("pulse"); }, 220);
  }

  // --- file list ---------------------------------------------------------

  // Refreshed on a slow interval as well as at startup: files get created and
  // deleted while the page is open, and a list that only loaded once would go
  // quietly out of date. Redrawing is skipped when nothing changed, so typing
  // in the filter box is never interrupted.
  function loadFileList() {
    return fetch("/api/list")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.files.join("\n") === allFiles.join("\n")) return;
        allFiles = data.files;
        drawFileList(data.truncated);
      })
      .catch(function () { /* surfaced by the poll's status indicator */ });
  }

  function drawFileList(truncated) {
    var needle = filterEl.value.toLowerCase();
    filesEl.innerHTML = "";
    allFiles.forEach(function (rel) {
      if (needle && rel.toLowerCase().indexOf(needle) === -1) return;
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = "#/" + encodeURIComponent(rel);
      a.textContent = rel;
      a.dataset.path = rel;
      li.appendChild(a);
      filesEl.appendChild(li);
    });
    if (truncated) {
      var note = document.createElement("li");
      note.style.cssText = "padding:8px 16px;font-size:12px;color:var(--fg-muted)";
      note.textContent = "listing truncated at " + allFiles.length + " files";
      filesEl.appendChild(note);
    }
    markActive();
  }

  filterEl.addEventListener("input", function () { drawFileList(false); });

  function markActive() {
    var links = filesEl.querySelectorAll("a");
    for (var i = 0; i < links.length; i++) {
      links[i].classList.toggle("active", links[i].dataset.path === current);
    }
  }

  // --- fetching a file ---------------------------------------------------

  function loadFile(rel, isRefresh) {
    fetch("/api/file?path=" + encodeURIComponent(rel))
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.error); });
        return r.json();
      })
      .then(function (payload) {
        render(payload);
        setStatus("live", "live");
        if (isRefresh) pulse();
      })
      .catch(function (err) {
        docEl.innerHTML = '<div class="notice">' + escapeHtml(String(err.message || err)) + "</div>";
        setStatus("stale", "error");
      });
  }

  // --- change detection --------------------------------------------------

  // Polling st_mtime costs microseconds per request and needs no open
  // connection, which makes it far simpler than watching FSEvents or holding a
  // websocket. At this interval it is indistinguishable from instant.
  function poll() {
    if (!current) return;
    fetch("/api/mtime?path=" + encodeURIComponent(current))
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.error); });
        return r.json();
      })
      .then(function (info) {
        if (lastMtime === null || info.mtime !== lastMtime) {
          loadFile(current, lastMtime !== null);
        } else {
          setStatus("live", "live");
        }
      })
      .catch(function (err) {
        setStatus("stale", err.message === "Failed to fetch" ? "server stopped" : "waiting");
      });
  }

  // --- routing -----------------------------------------------------------

  function targetFromHash() {
    var hash = location.hash;
    if (hash.indexOf("#/") !== 0) return null;
    return decodeURIComponent(hash.slice(2));
  }

  function go() {
    var next = targetFromHash() || INITIAL;
    if (!next) {
      pathEl.textContent = "no file selected";
      docEl.innerHTML = '<div class="notice">Pick a file from the list on the left.</div>';
      current = null;
      return;
    }
    if (next === current) return;
    current = next;
    lastMtime = null;
    pathEl.textContent = next;
    document.title = next.split("/").pop() + " - mdlive";
    markActive();
    loadFile(next, false);
  }

  window.addEventListener("hashchange", function () {
    // Ignore in-document anchor jumps (#section), which are not file routes.
    if (location.hash.indexOf("#/") === 0) go();
  });

  Promise.all([
    ensure("marked", CDN.marked),
    ensure("hljs", CDN.hljs)
  ]).then(function (results) {
    haveMarked = results[0];
    haveHljs = results[1];
    if (!haveMarked) setStatus("stale", "no renderer");
    loadFileList();
    go();
    setInterval(poll, POLL_MS);
    setInterval(loadFileList, LIST_REFRESH_MS);
  });
}());
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def start_server(
    host: str, preferred_port: int, root: Path, initial: Optional[str]
) -> PreviewServer:
    """Bind the first free port at or above `preferred_port`.

    Scanning forward rather than failing means mdlive still starts when another
    process holds the default port - necessary for a double-click handler that
    has no terminal to report an error to.
    """
    last_error: Optional[OSError] = None
    for port in range(preferred_port, preferred_port + PORT_SCAN_ATTEMPTS):
        try:
            return PreviewServer((host, port), root, initial)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            last_error = exc
    raise RuntimeError(
        f"no free port in {preferred_port}-{preferred_port + PORT_SCAN_ATTEMPTS - 1}"
    ) from last_error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mdlive",
        description="View Markdown in the browser and auto-refresh on every change.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="a Markdown file to open, or a directory to serve (default: .)",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"preferred port (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to bind (default: 127.0.0.1, this machine only)",
    )
    parser.add_argument("--no-open", action="store_true", help="do not open a browser on start")
    parser.add_argument(
        "--no-reuse",
        action="store_true",
        help="always start a new server instead of reusing a running one",
    )
    parser.add_argument("--version", action="version", version=f"mdlive {__version__}")
    return parser


def reuse_existing(target: Path, host: str, open_browser: bool) -> bool:
    """Hand a file to an already-running instance. True if one took it."""
    found = find_reusable_instance(target)
    if found is None:
        return False
    port, root = found
    relative = target.relative_to(root)
    url = f"http://{host}:{port}/#/{quote(str(relative))}"
    print(f"  reusing  server on port {port} (root {root})", flush=True)
    print(f"  open     {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    target = Path(args.target).expanduser().resolve()
    if target.is_file():
        if target.suffix.lower() not in MARKDOWN_SUFFIXES:
            parser.error(f"not a Markdown file: {target}")
        root, initial = target.parent, target.name
    elif target.is_dir():
        root, initial = target, None
    else:
        parser.error(f"no such file or directory: {target}")
        return 2  # unreachable; argparse.error exits

    if initial and not args.no_reuse and reuse_existing(target, args.host, not args.no_open):
        return 0

    server = start_server(args.host, args.port, root, initial)
    port = server.server_address[1]

    url = f"http://{args.host}:{port}/"
    if initial:
        url += "#/" + quote(initial)

    # flush explicitly: stdout is block-buffered when piped, which would
    # otherwise hide the banner until the process exits.
    print(f"  mdlive   {__version__}", flush=True)
    print(f"  serving  {root}", flush=True)
    print(f"  open     {url}", flush=True)
    print("  watching for changes... (Ctrl-C to stop)", flush=True)

    if not args.no_open:
        webbrowser.open(url)

    # Turn SIGTERM into a normal exit so the `finally` below still runs and the
    # registry entry is cleaned up when something kills us.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    register_instance(port, root)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped", flush=True)
    finally:
        unregister_instance(port)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
