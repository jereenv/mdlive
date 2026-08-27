#!/bin/bash
#
# Rasterise app/icon.svg into app/mdlive.icns.
#
# The resulting .icns is committed, so installing mdlive needs neither Chrome
# nor this script. Re-run it only after editing icon.svg.
#
# macOS ships no SVG rasteriser (`sips` cannot read SVG), so this borrows
# Chrome's renderer in headless mode. An .icns is a container of PNGs at fixed
# sizes; `sips` resizes and `iconutil` packs them.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [[ ! -x "$chrome" ]]; then
  echo "error: Google Chrome not found; needed only to rasterise the SVG" >&2
  exit 1
fi

echo "==> rendering icon.svg at 1024x1024"
"$chrome" --headless --disable-gpu --no-sandbox \
  --screenshot="$work/icon.png" \
  --window-size=1024,1024 \
  --default-background-color=00000000 \
  --hide-scrollbars \
  "file://$here/icon.svg" >/dev/null 2>&1

if [[ ! -f "$work/icon.png" ]]; then
  echo "error: Chrome produced no screenshot" >&2
  exit 1
fi

echo "==> building iconset"
iconset="$work/mdlive.iconset"
mkdir -p "$iconset"

# Every size an .icns is expected to carry. The @2x entries are the same pixel
# dimensions as the next size up, but Finder needs both names present.
add() { sips -z "$1" "$1" "$work/icon.png" --out "$iconset/$2" >/dev/null; }
add 16   icon_16x16.png
add 32   icon_16x16@2x.png
add 32   icon_32x32.png
add 64   icon_32x32@2x.png
add 128  icon_128x128.png
add 256  icon_128x128@2x.png
add 256  icon_256x256.png
add 512  icon_256x256@2x.png
add 512  icon_512x512.png
add 1024 icon_512x512@2x.png

iconutil --convert icns "$iconset" --output "$here/mdlive.icns"
echo "==> wrote $here/mdlive.icns"
