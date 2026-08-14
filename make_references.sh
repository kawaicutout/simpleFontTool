#!/usr/bin/env bash
# Render GF Latin Kernel reference grids (light/regular/bold, roman+italic)
# for the standard set of fonts. Output goes to ./out/<Font>_<Style>.svg/.png.
#
# Point FONT_DIR elsewhere if your fonts live in a different folder:
#   FONT_DIR=~/.fonts ./make_references.sh
set -euo pipefail

FONT_DIR="${FONT_DIR:-/usr/local/share/fonts}"

FILES=(
  m/Merriweather_Light.ttf          m/Merriweather_LightItalic.ttf
  m/Merriweather_Regular.ttf        m/Merriweather_Italic.ttf
  m/Merriweather_Bold.ttf           m/Merriweather_BoldItalic.ttf

  m/MerriweatherSans_Light.ttf      m/MerriweatherSans_LightItalic.ttf
  m/MerriweatherSans_Regular.ttf    m/MerriweatherSans_Italic.ttf
  m/MerriweatherSans_Bold.ttf       m/MerriweatherSans_BoldItalic.ttf

  l/LatoWeb_Light.ttf               l/LatoWeb_LightItalic.ttf
  l/LatoWeb_Regular.ttf             l/LatoWeb_Italic.ttf
  l/LatoWeb_Bold.ttf                l/LatoWeb_BoldItalic.ttf

  a/Aleo_Light.ttf                  a/Aleo_LightItalic.ttf
  a/Aleo_Regular.ttf                a/Aleo_Italic.ttf
  a/Aleo_Bold.ttf                   a/Aleo_BoldItalic.ttf

  i/IBMPlexSans_Light.ttf           i/IBMPlexSans_LightItalic.ttf
  i/IBMPlexSans_Regular.ttf         i/IBMPlexSans_Italic.ttf
  i/IBMPlexSans_Bold.ttf            i/IBMPlexSans_BoldItalic.ttf

  i/IBMPlexSerif_Light.ttf          i/IBMPlexSerif_LightItalic.ttf
  i/IBMPlexSerif_Regular.ttf        i/IBMPlexSerif_Italic.ttf
  i/IBMPlexSerif_Bold.ttf           i/IBMPlexSerif_BoldItalic.ttf

  i/IBMPlexMono_Light.ttf           i/IBMPlexMono_LightItalic.ttf
  i/IBMPlexMono_Regular.ttf         i/IBMPlexMono_Italic.ttf
  i/IBMPlexMono_Bold.ttf            i/IBMPlexMono_BoldItalic.ttf
)

for f in "${FILES[@]}"; do
  python3 glyph_grid.py "$FONT_DIR/$f"
done

echo "done — see ./out/"
