"""Regression coverage for static-asset-routing Slice 5 (event-creator#34): this app's static
assets must be reachable at both its own prefixed path (what chrome_base.html actually links to,
via CHROME_STATIC_PREFIX) and the old bare /static/* path (kept alive for the transition window -
see docs/adr/static-asset-routing-mount-transition.md in organize-me), mirroring the equivalent
test in organize-me's own app/main.py.
"""

from organizeme_chrome.static_paths import static_mount_path


def test_serves_static_assets_at_both_the_bare_and_prefixed_mount() -> None:
    from app.main import app

    mounted_paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert any(path.startswith(static_mount_path("event-creator")) for path in mounted_paths)
    assert any(
        path.startswith("/static") and not path.startswith(static_mount_path("event-creator"))
        for path in mounted_paths
    )
