# mdlive

A live-reloading Markdown viewer for macOS. Double-click a `.md` file and it
opens rendered in your browser, GitHub-styled — and it **re-renders itself the
moment the file changes on disk**. No reopening, no refreshing.

<!-- TODO(owner): record a short screen capture of editing a .md file in one
     window while this re-renders live in the browser next to it, convert to
     GIF, and drop it here, e.g.:
     ![mdlive re-rendering a file as it's edited](docs/demo.gif) -->

The problem this solves: you open a `.md` file to read it — notes, a design
doc, an agent's scratch output — and something keeps changing it out from
under you (you, in another editor; a formatter; a coding agent writing to
disk). Every other viewer makes you flip back and hit reload. mdlive polls the
file's mtime a few times a second and reflows only the document's HTML on
change, so it just stays current while you leave it open in a spare window.

Built for the case where something else is rewriting the file while you read
it: an editor, a formatter, or a code-generation agent.

- Zero dependencies. Python standard library only, no `pip`, no `npm`.
- Installs as a real Mac app that can own `.md` files.
- GitHub rendering: syntax-highlighted code, task lists, tables, heading
  anchors, copy buttons, light and dark following macOS appearance.
- One server per folder tree, reused automatically instead of piling up.

## Install

```bash
git clone https://github.com/jereenv/mdlive.git
cd mdlive
./install.sh
```

That installs three things, all inside your home folder, no `sudo`:

| What | Where |
|---|---|
| `mdlive` command | first writable dir already on your `PATH` |
| `mdlive.app` | `~/Applications/mdlive.app` |
| Default `.md` handler | LaunchServices association |

Pass `--no-default` to install without taking over `.md` files.

Remove everything with `./uninstall.sh`: it stops any running `mdlive`
servers, deletes the `mdlive` symlink and `mdlive.app`, unregisters the app
from LaunchServices, and clears the instance-registry cache under
`~/Library/Caches/mdlive`. It leaves this repository and your `.md` files
untouched; macOS may still remember mdlive as the last app used for `.md`
files until you pick another one (Get Info → Open with).

### Becoming the default app

On macOS 14 and later, which app owns a file type is treated as the user's
decision, not a script's. `install.sh` asks the system anyway, but the request
either raises a confirmation dialog or is ignored outright when there is no GUI
session — so it can't be fully automated. If the installer reports that `.md`
still belongs to another app, set it once in Finder:

1. Right-click any `.md` file, **Get Info**
2. Under **Open with**, pick **mdlive**
3. **Change All…**, confirm

`Open With ▸ mdlive` and the `mdlive` command work regardless.

## Use

```bash
mdlive                      # serve the current directory
mdlive notes.md             # open one file
mdlive ~/notes              # serve a tree, with a filterable file list
mdlive ~/notes --port 9000  # pick the port
```

Or just double-click any `.md` file in Finder.

Leave it running in a spare terminal tab. Every Markdown file under the served
root auto-refreshes on change, and your scroll position is preserved across
re-renders.

### Options

| Flag | Meaning |
|---|---|
| `--port N` | Preferred port, default `8765`. Scans forward if busy. |
| `--host ADDR` | Bind address, default `127.0.0.1` (this machine only). |
| `--no-open` | Don't open a browser on start. |
| `--no-reuse` | Always start a new server instead of reusing a running one. |
| `--version` | Print version. |

## How it works

```
browser  ──GET /api/mtime?path=notes.md──▶  mdlive.py  ──os.stat()──▶  disk
         ◀─────── {"mtime": 1787…} ───────
              (every 400ms; on change, refetch and re-render)
```

**Change detection is polling, not filesystem events.** The browser asks for
the file's modification time about twice a second. An `os.stat` costs
microseconds and needs no open connection, which is far simpler than watching
FSEvents (platform-specific, needs a third-party library) or holding a
WebSocket (connection lifecycle, reconnect logic, a thread per client). At this
interval it is indistinguishable from instant.

**Rendering happens in the browser**, via `marked` and `highlight.js`. That
keeps the server dependency-free, and means a re-render swaps only the
document's HTML — so scroll position survives. Both libraries load from
`./vendor/` if present and fall back to a CDN otherwise; with neither
reachable, the page degrades to showing the raw source instead of a blank page.

**Every response sends `Cache-Control: no-store`.** A tool whose entire value
is freshness must not let the browser serve a cached copy.

**Paths from the client are untrusted.** `_resolve()` resolves the requested
path and rejects anything that is not genuinely under the served root, so
neither `../../.ssh/id_rsa` nor a symlink planted inside the root can read
outside it. The server binds `127.0.0.1`, not `0.0.0.0`, so nothing off this
machine can reach it at all.

**`mdlive.app` is an AppleScript applet.** macOS delivers double-clicked
documents through an Apple Event, not `argv`, so a shell script cannot be a
document handler. The applet receives the file, shells out to the CLI, and
quits. The CLI then checks a small registry under
`~/Library/Caches/mdlive/servers/` and hands the file to an already-running
server whose root covers it, rather than starting a second one.

### Offline use

Drop the two libraries into `vendor/` and the CDN is never contacted:

```bash
mkdir -p vendor
curl -Lo vendor/marked.min.js https://cdn.jsdelivr.net/npm/marked@12/marked.min.js
curl -Lo vendor/highlight.min.js https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js
```

## Development

```bash
python3 -m unittest discover -s tests -v   # 31 tests, no dependencies
mdlive sample.md                           # rendering fixture, for eyeballing
./app/build-icon.sh                        # regenerate the icon (needs Chrome)
```

`mdlive.icns` is committed as a build artifact so installing needs neither
Chrome nor the icon script.

## Notes and limits

- macOS may ask once for permission when mdlive first reads a `.md` file in a
  protected location such as `~/Desktop` or `~/Documents`. That's TCC, the
  system privacy layer, not mdlive. The bundle is ad-hoc signed so the grant
  sticks across reinstalls.
- Opening a `.md` from Finder always opens a new browser tab; it does not
  refocus an existing one.
- Code fences without a declared language are left unhighlighted, matching
  GitHub. Auto-detection guesses wrong often enough to be worse than nothing.
- The sidebar lists at most 500 files, and says so when it truncates.

## License

MIT
