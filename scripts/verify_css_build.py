"""Fails loudly if the Tailwind build produced empty/near-empty output, or if the design tokens
didn't actually compile in - used by CI (ci.yml, deploy.yml) right after scripts/build_css.py so
a broken tokens.css import or a broken @source glob breaks the build instead of shipping an
unstyled app silently.
"""

import sys
from pathlib import Path

APP_CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "app.css"
MIN_BYTES = 1000

# Only exists in the compiled output if the "Signal" @theme tokens were actually picked up - a
# dropped tokens.css import or a broken @source glob both fail this check.
CANARY_CLASS = ".bg-flame"


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

    if CANARY_CLASS not in css:
        print(
            f"::error::canary class {CANARY_CLASS} is missing from app.css - "
            "design tokens likely failed to compile in"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
