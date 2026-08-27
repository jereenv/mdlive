#!/bin/bash
#
# Install mdlive: the `mdlive` command, the mdlive.app document handler, and
# the system association that makes double-clicking a .md file just work.
#
#   ./install.sh                 full install
#   ./install.sh --no-default    install without becoming the default .md app
#
# Everything lands in your home folder. No sudo, nothing touched outside
# ~/Applications, the chosen bin directory, and the LaunchServices database.

set -euo pipefail

BUNDLE_ID="com.jereenv.mdlive"
APP_NAME="mdlive.app"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cli="$here/mdlive.py"
app_dir="$HOME/Applications"
app="$app_dir/$APP_NAME"
set_default=1

for arg in "$@"; do
  case "$arg" in
    --no-default) set_default=0 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

version="$(/usr/bin/python3 "$cli" --version | awk '{print $2}')"
echo "==> installing mdlive $version"

# ---------------------------------------------------------------------------
# 1. the CLI
# ---------------------------------------------------------------------------

# Prefer a directory that is already on PATH so no shell config has to change.
pick_bin_dir() {
  local candidate
  for candidate in "$HOME/.local/bin" "/usr/local/bin" "$HOME/bin" "$HOME/go/bin"; do
    case ":$PATH:" in
      *":$candidate:"*) ;;
      *) continue ;;
    esac
    if [[ -d "$candidate" ]]; then
      # An existing directory must be writable. Note that `mkdir -p` would
      # succeed here regardless, since the directory already exists - so it
      # cannot be used as the writability test.
      [[ -w "$candidate" ]] || continue
    elif ! mkdir -p "$candidate" 2>/dev/null; then
      continue
    fi
    echo "$candidate"
    return 0
  done
  return 1
}

chmod +x "$cli"
if bin_dir="$(pick_bin_dir)"; then
  ln -sf "$cli" "$bin_dir/mdlive"
  echo "  cli      $bin_dir/mdlive"
else
  bin_dir="$HOME/.local/bin"
  mkdir -p "$bin_dir"
  ln -sf "$cli" "$bin_dir/mdlive"
  echo "  cli      $bin_dir/mdlive"
  echo "  NOTE     $bin_dir is not on your PATH. Add this to ~/.zshrc:"
  echo "             export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# ---------------------------------------------------------------------------
# 2. the app bundle
# ---------------------------------------------------------------------------

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The applet shells out to an absolute path: `do shell script` uses a
# non-interactive shell with no user PATH.
sed "s|__MDLIVE_PATH__|$cli|g" "$here/app/mdlive.applescript" > "$work/mdlive.applescript"

mkdir -p "$app_dir"
rm -rf "$app"
osacompile -o "$app" "$work/mdlive.applescript"
echo "  app      $app"

plist="$app/Contents/Info.plist"
pb() { /usr/libexec/PlistBuddy -c "$1" "$plist" >/dev/null; }
pb_soft() { /usr/libexec/PlistBuddy -c "$1" "$plist" >/dev/null 2>&1 || true; }
pb_set() { # key type value  -- Set if present, otherwise Add
  /usr/libexec/PlistBuddy -c "Set :$1 $3" "$plist" >/dev/null 2>&1 \
    || /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$plist" >/dev/null
}

pb_set CFBundleIdentifier string "$BUNDLE_ID"
pb_set CFBundleName string mdlive
pb_set CFBundleDisplayName string mdlive
pb_set CFBundleShortVersionString string "$version"
pb_set CFBundleVersion string "$version"
pb_set LSApplicationCategoryType string public.app-category.developer-tools
pb_set LSMinimumSystemVersion string 11.0
pb_set NSHumanReadableCopyright string "MIT licensed"

# Declare which documents this app can open. Without this, macOS will not
# offer mdlive in "Open With" and cannot make it the default.
pb_soft "Delete :CFBundleDocumentTypes"
pb "Add :CFBundleDocumentTypes array"
pb "Add :CFBundleDocumentTypes:0 dict"
pb "Add :CFBundleDocumentTypes:0:CFBundleTypeName string Markdown Document"
pb "Add :CFBundleDocumentTypes:0:CFBundleTypeRole string Viewer"
pb "Add :CFBundleDocumentTypes:0:LSHandlerRank string Owner"
pb "Add :CFBundleDocumentTypes:0:CFBundleTypeIconFile string applet"
pb "Add :CFBundleDocumentTypes:0:LSItemContentTypes array"
pb "Add :CFBundleDocumentTypes:0:LSItemContentTypes:0 string net.daringfireball.markdown"
pb "Add :CFBundleDocumentTypes:0:CFBundleTypeExtensions array"
i=0
for ext in md markdown mdown mkd; do
  pb "Add :CFBundleDocumentTypes:0:CFBundleTypeExtensions:$i string $ext"
  i=$((i + 1))
done

# Import the Markdown type rather than exporting it: the identifier is
# Daring Fireball's convention, and several apps already declare it. Importing
# guarantees it resolves even on a Mac where nothing else has.
pb_soft "Delete :UTImportedTypeDeclarations"
pb "Add :UTImportedTypeDeclarations array"
pb "Add :UTImportedTypeDeclarations:0 dict"
pb "Add :UTImportedTypeDeclarations:0:UTTypeIdentifier string net.daringfireball.markdown"
pb "Add :UTImportedTypeDeclarations:0:UTTypeDescription string Markdown Document"
pb "Add :UTImportedTypeDeclarations:0:UTTypeConformsTo array"
pb "Add :UTImportedTypeDeclarations:0:UTTypeConformsTo:0 string public.plain-text"
pb "Add :UTImportedTypeDeclarations:0:UTTypeTagSpecification dict"
pb "Add :UTImportedTypeDeclarations:0:UTTypeTagSpecification:public.filename-extension array"
i=0
for ext in md markdown mdown mkd; do
  pb "Add :UTImportedTypeDeclarations:0:UTTypeTagSpecification:public.filename-extension:$i string $ext"
  i=$((i + 1))
done

cp "$here/app/mdlive.icns" "$app/Contents/Resources/applet.icns"
echo "  icon     installed"

# Ad-hoc signature. Unsigned bundles get a new identity every time they are
# rebuilt, which makes macOS re-ask for file-access permission on each install;
# a stable ad-hoc signature avoids that.
if codesign --force --sign - "$app" >/dev/null 2>&1; then
  echo "  signed   ad-hoc"
else
  echo "  WARN     ad-hoc signing failed; macOS may re-prompt for file access"
fi

# ---------------------------------------------------------------------------
# 3. register with the system
# ---------------------------------------------------------------------------

"$LSREGISTER" -f "$app"
echo "  register LaunchServices updated"

default_is_ours=0
if (( set_default )); then
  echo "==> making mdlive the default app for Markdown"
  # This can legitimately fail. macOS 14+ treats "which app opens this file
  # type" as the user's decision, so the request may raise a confirmation
  # dialog and will not complete at all without a GUI session. The script
  # reports what the system actually resolves rather than trusting the API's
  # return value, which reports success either way.
  if swift "$here/app/set-default-handler.swift" "$app"; then
    default_is_ours=1
  fi
else
  echo "==> skipping default-app registration (--no-default)"
fi

cat <<EOF

Done.

  mdlive ~/personal      serve a whole folder, with a file list
  mdlive notes.md        open one file
EOF

if (( default_is_ours )); then
  cat <<EOF
  double-click any .md   opens in mdlive, live-reloading
EOF
else
  current="$(/usr/bin/swift - <<'SWIFT' 2>/dev/null || true
import AppKit
import UniformTypeIdentifiers
if let t = UTType(filenameExtension: "md"),
   let u = NSWorkspace.shared.urlForApplication(toOpen: t) {
    print(u.deletingPathExtension().lastPathComponent)
}
SWIFT
)"
  cat <<EOF

One step left. macOS 14+ treats the default app for a file type as the user's
decision, not a script's, so .md files still open in ${current:-another app}.
Either re-run ./install.sh while sitting at an unlocked Mac and confirm the
dialog macOS shows, or set it directly, once:

  1. Right-click any .md file in Finder, choose "Get Info"
  2. Under "Open with", pick mdlive
  3. Click "Change All..." and confirm

Until then, "Open With > mdlive" works, and so does the mdlive command.
EOF
fi

echo
echo "Uninstall with ./uninstall.sh"
