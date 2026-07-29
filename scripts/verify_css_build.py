"""Fails loudly if the Tailwind build produced empty/near-empty output, or if the design tokens
didn't actually compile in - used by CI (ci.yml, deploy.yml) right after scripts/build_css.py so
a broken tokens.css import or a broken @source glob breaks the build instead of shipping an
unstyled app silently.
"""

import sys
from pathlib import Path

APP_CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "app.css"
MIN_BYTES = 1000

# Only exist in the compiled output if the relevant @source glob actually got scanned. ".bg-flame"
# alone wasn't enough to catch a real incident (organize-me#... event-creator dashboard's reviewed
# toggle/import/bulk-review buttons going invisible): a Tailwind v4.3.3 CLI bug drops classes from
# some (not all) subdirectories when a single recursive `**` glob is rooted inside a directory this
# repo's own .gitignore excludes (`.venv`) and that root has more than one child directory -
# ".bg-flame" happened to survive (it's the only chrome/design/classes.py-only class that was also
# literal in an events_panel.html string at the time), but ".bg-amber" (only ever a Python dict
# value in chrome/design/classes.py, never literal in any .html) and ".w-11" (only literal in
# chrome/templates/components/toggle.html, a "components" subdirectory sibling to "macros") both
# went missing. scripts/build_css.py works around the CLI bug by sourcing each subdirectory
# separately instead of one glob spanning all of them - these two extra canaries would have failed
# loudly instead of shipping invisible buttons/toggles.
CANARY_CLASSES = (".bg-flame", ".bg-amber", ".w-11")


def main() -> int:
    if not APP_CSS.is_file():
        print(f"::error::{APP_CSS} does not exist - did the Tailwind build step run?")
        return 1

    css = APP_CSS.read_text(encoding="utf-8")
    size = len(css.encode("utf-8"))
    print(f"Compiled app.css is {size} bytes")

    if size < MIN_BYTES:
        print(
            f"::error::app.css is suspiciously small ({size} bytes) - "
            "Tailwind build likely produced empty/near-empty output"
        )
        return 1

    missing = [c for c in CANARY_CLASSES if c not in css]
    if missing:
        print(
            f"::error::canary class(es) {', '.join(missing)} missing from app.css - "
            "design tokens or a @source glob likely failed to compile in"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
