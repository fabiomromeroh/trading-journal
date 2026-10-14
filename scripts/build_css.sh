#!/bin/bash
# Prebuilt Tailwind CSS (replaces the in-browser Play CDN). Downloads the free standalone CLI if missing,
# writes app/static/tw.<hash>.css and app/static/tw.manifest (the file name base.html links to).
set -e
cd "$(dirname "$0")/.."
TW=${TAILWIND_BIN:-/tmp/tailwindcss}
[ -x "$TW" ] || { curl -sL -o "$TW" https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-linux-x64; chmod +x "$TW"; }
"$TW" -c tailwind.config.js -i app/static/tailwind.src.css -o /tmp/tw.css --minify 2>/dev/null
H=$(sha1sum /tmp/tw.css | cut -c1-10)
rm -f app/static/tw.*.css
mv /tmp/tw.css app/static/tw.$H.css
echo "tw.$H.css" > app/static/tw.manifest
echo "built app/static/tw.$H.css ($(wc -c < app/static/tw.$H.css) bytes)"
