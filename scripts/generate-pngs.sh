#!/usr/bin/env bash
# mcp-awareness — ambient system awareness for AI agents
# Copyright (C) 2026 Chris Means
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# Generate PNG exports from SVG branding assets.
# Requires: magick (ImageMagick 7+)
set -euo pipefail

BRANDING_DIR="$(cd "$(dirname "$0")/../docs/branding" && pwd)"
PNG_DIR="${BRANDING_DIR}/png"
mkdir -p "$PNG_DIR"

echo "Generating PNGs from SVGs..."

# Sizes that have hand-tuned SVGs
SIZES=(16 32 48 64 100 200)

for size in "${SIZES[@]}"; do
    for theme in "" "-dark"; do
        src="${BRANDING_DIR}/awareness-logo-${size}${theme}.svg"
        dst="${PNG_DIR}/awareness-${size}${theme}.png"
        if [ -f "$src" ]; then
            magick -background none -density 300 "$src" -resize "${size}x${size}" "$dst"
            echo "  ${dst##*/}"
        fi
    done
done

# Upscaled sizes from 200px SVG
for size in 256 512; do
    for theme in "" "-dark"; do
        src="${BRANDING_DIR}/awareness-logo-200${theme}.svg"
        dst="${PNG_DIR}/awareness-${size}${theme}.png"
        if [ -f "$src" ]; then
            magick -background none -density 300 "$src" -resize "${size}x${size}" "$dst"
            echo "  ${dst##*/}"
        fi
    done
done

# Wordmark PNGs
for theme in "" "-dark"; do
    src="${BRANDING_DIR}/awareness-logo-wordmark${theme}.svg"
    dst="${PNG_DIR}/awareness-wordmark${theme}.png"
    if [ -f "$src" ]; then
        magick -background none -density 300 "$src" "$dst"
        echo "  ${dst##*/}"
    fi
done

echo "Done. PNGs in ${PNG_DIR}"
