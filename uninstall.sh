#!/bin/bash
#
# Remove everything install.sh created.

set -euo pipefail

APP_NAME="mdlive.app"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

app="$HOME/Applications/$APP_NAME"

echo "==> uninstalling mdlive"

# Stop any running instances. They are plain python processes owned by you.
if pkill -f "mdlive.py" 2>/dev/null; then
  echo "  stopped  running servers"
fi

for dir in "$HOME/.local/bin" "/usr/local/bin" "$HOME/bin" "$HOME/go/bin"; do
  if [[ -L "$dir/mdlive" ]]; then
    rm -f "$dir/mdlive"
    echo "  removed  $dir/mdlive"
  fi
done

if [[ -d "$app" ]]; then
  "$LSREGISTER" -u "$app" >/dev/null 2>&1 || true
  rm -rf "$app"
  echo "  removed  $app"
fi

rm -rf "$HOME/Library/Caches/mdlive"
echo "  removed  instance registry"

"$LSREGISTER" -kill -r -domain local -domain system -domain user >/dev/null 2>&1 || true

cat <<'EOF'

Done. The repository itself is untouched.

macOS remembers the last app used for a file type, so .md files may still
point at mdlive until you pick another: right-click a .md file, Get Info,
"Open with", choose an app, then "Change All...".
EOF
