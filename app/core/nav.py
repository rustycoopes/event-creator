from fastapi import Request
from organizeme_chrome import NavGroup, build_nav_groups, flat_nav_items, list_apps

from app.models.host_user import HostUser


def sidebar_nav_context(host_user: HostUser | None, request: Request) -> dict[str, object]:
    """Per-request sidebar context: grouped nav, flat nav, and the collapsed-state maps.

    Mirrors organize-me's `app.core.nav.sidebar_nav_context` (see
    docs/adr/sidebar-nav-groups-render-boundary.md in the organize-me repo), but reads the user's
    collapsed-group preference from the read-only `HostUser` mapping instead of a writable `User`
    model - event-creator has no write path to persist it, so `nav_stored_collapsed_json` here is
    only ever the Host's real stored value (never a locally-mutated one) even though the shared
    `chrome_authenticated_base.html` template still wires its toggle button to PATCH
    `/api/v1/users/me` directly - that path-based route is served by the Host regardless of which
    service rendered the page (see docs/host-integration-guide.md's URL-map routing), so the
    toggle works correctly here without any event-creator-side write support.

    `host_user` is `None` only in the defensive case `get_host_user()` already handles (a JWT for a
    Host user id that no longer resolves to a row) - falls back to nothing collapsed.
    """
    apps = list_apps()
    collapsed = host_user.nav_collapsed_groups if host_user is not None else {}
    nav_groups: list[NavGroup] = build_nav_groups(
        apps, collapsed=collapsed, current_path=request.url.path
    )
    return {
        "nav_groups": nav_groups,
        "flat_nav_items": flat_nav_items(apps),
        "nav_collapsed_json": {group.service_name: group.collapsed for group in nav_groups},
        "nav_stored_collapsed_json": {
            group.service_name: collapsed.get(group.service_name, False) for group in nav_groups
        },
    }
