# Rendering sample

A fixture for eyeballing the renderer. Everything below should look the way it
does on GitHub.

## Task lists

- [x] Poll `st_mtime` instead of watching FSEvents
- [x] Send `Cache-Control: no-store`
- [ ] Reuse an existing browser tab
- [ ] Ship a Quick Look generator

## Code

```python
def find_reusable_instance(target: Path) -> Optional[Tuple[int, Path]]:
    """Find a running instance whose served root contains `target`."""
    for entry in sorted(REGISTRY_DIR.glob("*.json")):
        root = probe_instance(int(entry.stem))
        if root is not None and root in target.parents:
            return int(entry.stem), root
    return None
```

```go
func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/mtime", func(w http.ResponseWriter, r *http.Request) {
		info, err := os.Stat(r.URL.Query().Get("path"))
		if err != nil {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		json.NewEncoder(w).Encode(map[string]int64{"mtime": info.ModTime().Unix()})
	})
	log.Fatal(http.ListenAndServe("127.0.0.1:8765", mux))
}
```

```bash
mdlive ~/personal --port 9000 --no-open
```

```json
{ "root": "/Users/you/notes", "pid": 4773, "version": "1.1.0" }
```

A fence with no language declared stays plain, same as GitHub:

```
  serving  /Users/you/notes
  open     http://127.0.0.1:8765/#/notes.md
```

Inline `code`, **bold**, *italic*, ~~struck~~, and a [link](https://example.com).
Press <kbd>Ctrl</kbd> + <kbd>C</kbd> to stop the server.

## Table

| Approach | Latency | Complexity |
|---|---:|---|
| Poll `st_mtime` | ~400 ms | low |
| FSEvents | instant | medium |
| WebSocket push | instant | high |

## Quote and nesting

> A tool whose entire value is freshness must not let the browser serve a
> cached copy.
>
> 1. Read the file
> 2. Compare the timestamp
>    - unchanged: do nothing
>    - changed: re-render

---

### Deeper heading

Hover any heading to reveal its anchor link.
