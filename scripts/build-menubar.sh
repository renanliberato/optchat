#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${OPTCHAT_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then PYTHON="$(command -v python3)"; fi
"$PYTHON" -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
export CLANG_MODULE_CACHE_PATH="$ROOT/build/clang-cache"
export SWIFTPM_MODULECACHE_OVERRIDE="$ROOT/build/swift-cache"
swift build --disable-sandbox --package-path "$ROOT/macos" --scratch-path "$ROOT/build/swift" -c release
APP="$ROOT/dist/OptChatBar.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/python"
cp "$ROOT/build/swift/release/OptChatBar" "$APP/Contents/MacOS/OptChatBar"
cp -R "$ROOT/optchat" "$APP/Contents/Resources/python/"
"$PYTHON" - "$APP" "$PYTHON" <<'PY'
import json, plistlib, sys
from pathlib import Path
app = Path(sys.argv[1])
(app / 'Contents/Resources/runtime.json').write_text(json.dumps({'python': sys.argv[2]}))
with (app / 'Contents/Info.plist').open('wb') as file:
    plistlib.dump({'CFBundleExecutable': 'OptChatBar', 'CFBundleIdentifier': 'local.optchat.bar',
                  'CFBundleName': 'OptChatBar', 'CFBundleDisplayName': 'OptChat',
                  'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '0.1.0',
                  'CFBundleVersion': '1', 'LSUIElement': True, 'LSMinimumSystemVersion': '14.0',
                  'NSHighResolutionCapable': True}, file)
PY
codesign --force --sign - "$APP"
printf '%s\n' "$APP"
