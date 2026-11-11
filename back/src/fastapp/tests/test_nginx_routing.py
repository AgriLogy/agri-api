"""Every fastapp route must be reachable through nginx.

fastapp is a strangler sidecar: a route only exists for clients once
``deploy/nginx/*.conf`` sends its path to the fastapp upstream. Anything not
matched falls through to the Django catch-all (``location /``) — and when Django
has no such URL, production answers 404 while every unit test (which calls
fastapp directly) stays green. That is how ``/my-devices``, ``/reports/*`` and
``/users/{username}/access-level`` shipped broken (agri-api #460 e2e run).

This test closes the gap: it parses the nginx configs, applies nginx's own
location-selection rules to every path in fastapp's OpenAPI schema, and fails
for any path that would not reach fastapp — unless it is listed below with the
reason it deliberately stays on Django or is internal-only.

Pure: no network, no nginx binary, no database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from fastapp.main import app

DEPLOY = Path(__file__).resolve().parents[4] / "deploy" / "nginx"

# conf file -> the fastapp upstream port it proxies to.
CONFS = {"back.conf": 8001, "staging.conf": 9001}

# Paths fastapp serves that are deliberately NOT routed to it. Each needs a
# reason; adding a path here is a decision, not a way to silence the test.
NOT_ROUTED_TO_FASTAPP: dict[str, str] = {
    # Internal: hit directly on the container port (healthchecks, debugging).
    "/healthz": "container healthcheck, called on :8001 directly",
    "/fast/whoami": "debug probe, called on :8001 directly",
    # Held on Django: the fastapp port cannot reproduce Django's verbose_name /
    # help_text / choices metadata that agri-admin's Database page renders.
    "/admin/db/tables": "held on Django (field metadata) — see back.conf F6-admin-db",
    "/admin/db/tables/{key}/rows": "held on Django — see back.conf F6-admin-db",
    "/admin/db/tables/{key}/rows/{pk}": "held on Django — see back.conf F6-admin-db",
    "/admin/db/tables/{key}/schema": "held on Django — see back.conf F6-admin-db",
    # Auth issuance is still served by Django (byte-parity ports kept in fastapp).
    "/auth/admin-sessions": "served by Django",
    "/auth/sessions": "served by Django",
    "/auth/signup": "served by Django",
    "/auth/token/": "served by Django",
    "/auth/token/refresh/": "served by Django",
    # /users/* admin surface is still served by Django (parity ports in fastapp).
    "/users": "served by Django",
    "/users/me/change-password": "served by Django",
    "/users/me/notifications": "served by Django",
    "/users/{username}": "served by Django",
    "/users/{username}/activate": "served by Django",
    "/users/{username}/activity": "served by Django",
    "/users/{username}/alerts": "served by Django",
    "/users/{username}/force-logout": "served by Django",
    "/users/{username}/password-reset": "served by Django",
    "/users/{username}/sensor-units": "served by Django",
    "/users/{username}/sessions": "served by Django",
    "/users/{username}/zones": "served by Django",
    "/users/{username}/zones/{pk}": "served by Django",
    "/users/{username}/zones/{pk}/params": "served by Django",
    "/users/{username}/zones/{zone_id}/active-graph": "served by Django",
    "/users/{username}/zones/{zone_id}/graph-names": "served by Django",
    "/users/{username}/zones/{zone_id}/sensor-colors": "served by Django",
}


@dataclass(frozen=True)
class Location:
    modifier: str  # "=", "^~", "~", "~*" or "" (plain prefix)
    pattern: str
    port: int | None  # upstream port of its proxy_pass


_LOCATION = re.compile(r"location\s+(=|\^~|~\*|~)?\s*(\S+)\s*\{([^}]*)\}")
_PROXY_PORT = re.compile(r"proxy_pass\s+http://[^:;/]+:(\d+)")


def parse_locations(text: str) -> list[Location]:
    """Every active ``location`` block, in file order (comments stripped)."""
    text = re.sub(r"#[^\n]*", "", text)
    out = []
    for modifier, pattern, body in _LOCATION.findall(text):
        port = _PROXY_PORT.search(body)
        out.append(
            Location(modifier or "", pattern, int(port.group(1)) if port else None)
        )
    return out


def select(locations: list[Location], uri: str) -> Location | None:
    """nginx's location selection: exact ``=`` match wins; otherwise the
    longest prefix is remembered, and used directly if it is ``^~``; otherwise
    regexes are tried in file order and the first match wins; otherwise the
    longest prefix."""
    for loc in locations:
        if loc.modifier == "=" and loc.pattern == uri:
            return loc
    prefixes = [
        loc
        for loc in locations
        if loc.modifier in ("", "^~") and uri.startswith(loc.pattern)
    ]
    longest = max(prefixes, key=lambda loc: len(loc.pattern), default=None)
    if longest is not None and longest.modifier == "^~":
        return longest
    for loc in locations:
        if loc.modifier in ("~", "~*"):
            flags = re.IGNORECASE if loc.modifier == "~*" else 0
            if re.search(loc.pattern, uri, flags):
                return loc
    return longest


def _concrete(path: str) -> str:
    """A request URI for an OpenAPI path template: ``{x}`` → a sample segment."""
    return re.sub(r"\{[^}]+\}", "sample1", path)


FASTAPP_PATHS = sorted(app.openapi()["paths"])


def test_the_selection_rules_match_nginx():
    locs = parse_locations(
        """
        location = /a { proxy_pass http://h:1; }
        location /a/ { proxy_pass http://h:2; }
        location ^~ /static/ { proxy_pass http://h:3; }
        location ~ ^/a/[^/]+/x$ { proxy_pass http://h:4; }
        location ~ \\.png$ { proxy_pass http://h:5; }
        # location /commented/ { proxy_pass http://h:9; }
        location / { proxy_pass http://h:6; }
        """
    )
    assert select(locs, "/a").port == 1
    assert select(locs, "/a/b").port == 2
    assert select(locs, "/a/b/x").port == 4  # regex beats a plain prefix
    assert select(locs, "/static/i.png").port == 3  # ^~ stops the regex search
    assert select(locs, "/commented/z").port == 6  # comments are ignored
    assert select(locs, "/zzz").port == 6


def test_the_allowlist_has_no_stale_entries():
    stale = sorted(set(NOT_ROUTED_TO_FASTAPP) - set(FASTAPP_PATHS))
    assert not stale, f"no longer fastapp routes, remove from the allowlist: {stale}"


@pytest.mark.parametrize("conf", sorted(CONFS))
def test_every_fastapp_route_reaches_fastapp(conf):
    locations = parse_locations((DEPLOY / conf).read_text())
    fast_port = CONFS[conf]
    unrouted = []
    for path in FASTAPP_PATHS:
        if path in NOT_ROUTED_TO_FASTAPP:
            continue
        chosen = select(locations, _concrete(path))
        if chosen is None or chosen.port != fast_port:
            unrouted.append(f"{path} -> {chosen.pattern if chosen else 'nothing'}")
    assert not unrouted, (
        f"{conf}: these fastapp routes fall through to Django (404 in "
        "production). Add a location for them, or list them in "
        "NOT_ROUTED_TO_FASTAPP with the reason:\n  " + "\n  ".join(unrouted)
    )
