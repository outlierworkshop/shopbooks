#!/bin/bash
# Build a signed ShopBooks.app (Apple Silicon). Output: dist/ShopBooks.app + dist/ShopBooks-mac-arm64.zip
#
#   ./build-mac.sh                       # ad-hoc signed (default) — fine for your own Macs;
#                                        #   first launch on another Mac: right-click -> Open
#   IDENTITY="Developer ID Application: Your Name (TEAMID)" ./build-mac.sh
#                                        # real signature (hardened runtime + timestamp)
#   IDENTITY="Developer ID …" NOTARIZE=1 ./build-mac.sh
#                                        # + notarize & staple. One-time setup first:
#                                        #   xcrun notarytool store-credentials shopbooks-notary \
#                                        #     --apple-id you@example.com --team-id TEAMID
#
# The .app bundles its own Python (3.13) + deps; the books stay in
# ~/Library/Application Support/ShopBooks, untouched by app updates.
set -euo pipefail
cd "$(dirname "$0")"
IDENTITY="${IDENTITY:--}"

# 1. Throwaway build venv on Python 3.13 (the repo's dev .venv stays as-is).
if [ ! -x build/venv/bin/python ]; then
  echo "== creating build venv (Python 3.13) =="
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.13 build/venv
    uv pip install --python build/venv/bin/python -q -r requirements.txt pyinstaller
  else
    python3 -m venv build/venv
    build/venv/bin/pip -q install -r requirements.txt pyinstaller
  fi
fi

# 2. Icon: static/app-icon.png (the ShopBooks "$" mark) -> build/ShopBooks.icns
#    (sips + iconutil, no extra deps). Always regenerated so a changed source icon can never
#    leave a stale .icns behind.
echo "== generating ShopBooks.icns =="
rm -f build/ShopBooks.icns
rm -rf build/icon.iconset && mkdir -p build/icon.iconset
for s in 16 32 64 128 256 512; do
  sips -z "$s" "$s" static/app-icon.png --out "build/icon.iconset/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z "$d" "$d" static/app-icon.png --out "build/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns build/icon.iconset -o build/ShopBooks.icns

# 3. Bundle.
echo "== running PyInstaller =="
build/venv/bin/pyinstaller --noconfirm shopbooks.spec

# 4. Sign. Ad-hoc ("-") for personal machines; a Developer ID adds the hardened runtime.
echo "== signing (identity: $IDENTITY) =="
if [ "$IDENTITY" = "-" ]; then
  codesign --force --deep -s - dist/ShopBooks.app
else
  codesign --force --deep --options runtime --timestamp -s "$IDENTITY" dist/ShopBooks.app
fi
codesign --verify --deep --strict dist/ShopBooks.app
echo "   signature verified"

# 5. Zip artifact (ditto preserves the bundle structure + signatures).
ditto -c -k --keepParent dist/ShopBooks.app dist/ShopBooks-mac-arm64.zip

# 6. Optional notarization (real identity only; see header for the one-time credential setup).
if [ "$IDENTITY" != "-" ] && [ "${NOTARIZE:-0}" = "1" ]; then
  echo "== notarizing =="
  xcrun notarytool submit dist/ShopBooks-mac-arm64.zip --keychain-profile shopbooks-notary --wait
  xcrun stapler staple dist/ShopBooks.app
  ditto -c -k --keepParent dist/ShopBooks.app dist/ShopBooks-mac-arm64.zip  # re-zip stapled app
fi

echo "== done: dist/ShopBooks.app  (zip: dist/ShopBooks-mac-arm64.zip) =="
