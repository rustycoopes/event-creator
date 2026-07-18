"""Compiles Event Creator's Tailwind CSS build.

Resolves the installed organizeme_chrome package's tokens.css/templates/fonts at run time
(instead of hardcoding a guessed site-packages path) and runs pytailwindcss against a generated
entry file whose content globs cover both this app's own templates and chrome's shared ones -
see docs/adr/design-refresh-per-service-tailwind-build.md (organize-me).

Usage:
    uv run python scripts/build_css.py            # one-shot minified build (Docker build stage)
    uv run python scripts/build_css.py --watch     # local dev, run alongside `uvicorn --reload`
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from organizeme_chrome.paths import chrome_fonts_dir, chrome_templates_dir, chrome_tokens_css_path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_TEMPLATES_DIR = REPO_ROOT / "app" / "templates"
STATIC_CSS_DIR = REPO_ROOT / "app" / "static" / "css"
STATIC_FONTS_DIR = REPO_ROOT / "app" / "static" / "fonts"
GENERATED_ENTRY = STATIC_CSS_DIR / ".generated-entry.css"
OUTPUT_CSS = STATIC_CSS_DIR / "app.css"

# Pin the Tailwind CLI release explicitly so dev/CI/Docker can't silently drift onto different
# CLI versions with different output. Bump deliberately; see
# https://github.com/tailwindlabs/tailwindcss/releases.
TAILWINDCSS_VERSION = "v4.3.3"


def _write_entry_css() -> None:
    entry = "\n".join(
        [
            '@import "tailwindcss";',
            f'@source "{APP_TEMPLATES_DIR.as_posix()}/**/*.html";',
            f'@source "{chrome_templates_dir().as_posix()}/**/*.html";',
            f'@import "{chrome_tokens_css_path().as_posix()}";',
        ]
    )
    GENERATED_ENTRY.write_text(entry + "\n")


def _copy_fonts() -> None:
    """Fonts are chrome package data; app.css's @font-face rules reference them at
    /static/fonts/<name>, so they must physically exist in this app's own static tree, same as
    the compiled stylesheet."""
    STATIC_FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for font_file in chrome_fonts_dir().glob("*.woff2"):
        shutil.copy2(font_file, STATIC_FONTS_DIR / font_file.name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="Recompile on template/CSS changes")
    args = parser.parse_args()

    STATIC_CSS_DIR.mkdir(parents=True, exist_ok=True)
    _write_entry_css()
    _copy_fonts()

    env = os.environ.copy()
    env.setdefault("TAILWINDCSS_VERSION", TAILWINDCSS_VERSION)

    cmd = ["tailwindcss", "-i", str(GENERATED_ENTRY), "-o", str(OUTPUT_CSS)]
    cmd += ["--watch"] if args.watch else ["--minify"]

    try:
        subprocess.run(cmd, check=True, env=env, cwd=REPO_ROOT)
    except KeyboardInterrupt:
        # Expected way to stop --watch; not a build failure.
        pass


if __name__ == "__main__":
    main()
