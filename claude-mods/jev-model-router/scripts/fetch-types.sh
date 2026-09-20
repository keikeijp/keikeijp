#!/bin/sh
# Downloads the `claude-code` type declarations the hooks are written against.
# Inside a Claude Code session, `/plugin-types` writes the same file for the
# exact version you run; this script is for CI and editors.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$here/types"
url="https://raw.githubusercontent.com/anthropics/claude-code/main/mods/types/claude-code.d.ts"
curl -sSfL "$url" -o "$here/types/claude-code.d.ts"
echo "wrote $here/types/claude-code.d.ts ($(wc -c < "$here/types/claude-code.d.ts") bytes)"
