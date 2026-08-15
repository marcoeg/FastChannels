#!/usr/bin/env python3
"""
Assert that docs/openapi.yaml covers every JSON-returning route.

Written after a real miss: the original check excluded `/admin` wholesale on
the assumption that those routes render HTML. Two of them return JSON —
`/admin/guide/programs`, which is the only route that returns programme data
selected by channel ID, and `/admin/channels/chnum-map`. Because the exclusion
was baked into the test, it reported zero drift while both were undocumented.

So: classify every route by what it actually returns, and require that
anything returning JSON is documented. HTML routes are reported but not
enforced.

Usage:
    python3 dev/check_api_docs.py          # exits non-zero on drift
"""
from __future__ import annotations

import pathlib
import re
import sys

import yaml

ROUTES_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "routes"
SPEC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml"

BLUEPRINT_PREFIX = {
    "api_bp": "/api",
    "feeds_api_bp": "/api/feeds",
    "admin_bp": "/admin",
    "output_bp": "",
    "play_bp": "",
    "images_bp": "",
}

# Decorator form:  @api_bp.route('/x')
DECORATOR = re.compile(r"@(\w+_bp)\.route\(\s*'([^']*)'")
# Statement form:  api_bp.add_url_rule('/x', 'endpoint', view_func)
# These are NOT decorators — an earlier version of this check required a
# leading '@' and silently missed six TVE alias routes.
ADD_URL_RULE = re.compile(r"^\s*(\w+_bp)\.add_url_rule\(\s*'([^']*)'", re.M)


def flask_to_openapi(rule: str) -> str:
    return re.sub(r"<(?:[a-z]+:)?([^>]+)>", r"{\1}", rule)


def classify(source: str, start: int, blueprint: str) -> str:
    """What does the handler that starts at *start* return?"""
    nxt = source.find(f"@{blueprint}.", start + 1)
    body = source[start:nxt] if nxt != -1 else source[start:]
    if re.search(r"\breturn jsonify\(", body):
        return "JSON"
    if "render_template" in body:
        return "HTML"
    return "OTHER"


def collect_routes() -> dict[str, str]:
    """Map OpenAPI-style path -> JSON | HTML | OTHER."""
    routes: dict[str, str] = {}
    for path in sorted(ROUTES_DIR.glob("*.py")):
        src = path.read_text()

        for m in DECORATOR.finditer(src):
            bp, rule = m.group(1), m.group(2)
            prefix = BLUEPRINT_PREFIX.get(bp)
            if prefix is None:
                continue
            key = flask_to_openapi((prefix + rule) or "/")
            kind = classify(src, m.start(), bp)
            # A path with several decorators keeps the strongest classification.
            if routes.get(key) != "JSON":
                routes[key] = kind

        for m in ADD_URL_RULE.finditer(src):
            bp, rule = m.group(1), m.group(2)
            prefix = BLUEPRINT_PREFIX.get(bp)
            if prefix is None:
                continue
            key = flask_to_openapi((prefix + rule) or "/")
            # add_url_rule reuses an existing view function, so the body cannot
            # be classified from here. These are all JSON aliases in practice;
            # inherit from the target path if we have seen it, else assume JSON.
            routes.setdefault(key, "JSON")
    return routes


def main() -> int:
    spec = yaml.safe_load(SPEC.read_text())
    documented = set(spec["paths"])
    routes = collect_routes()

    json_routes = {p for p, k in routes.items() if k == "JSON"}
    html_routes = {p for p, k in routes.items() if k == "HTML"}
    other_routes = {p for p, k in routes.items() if k == "OTHER"}

    undocumented = sorted(json_routes - documented)
    phantom = sorted(documented - set(routes))

    print(f"spec paths        : {len(documented)}")
    print(f"routes in code    : {len(routes)}  "
          f"(JSON {len(json_routes)}, HTML {len(html_routes)}, other {len(other_routes)})")
    print()

    ok = True
    if undocumented:
        ok = False
        print(f"FAIL — {len(undocumented)} JSON route(s) not in the spec:")
        for p in undocumented:
            print(f"   {p}")
    else:
        print("OK — every JSON route is documented")

    if phantom:
        ok = False
        print(f"\nFAIL — {len(phantom)} documented path(s) with no matching route:")
        for p in phantom:
            print(f"   {p}")
    else:
        print("OK — every documented path exists in the code")

    undocumented_html = sorted(html_routes - documented)
    if undocumented_html:
        print(f"\nnote — {len(undocumented_html)} HTML route(s) intentionally out of scope:")
        for p in undocumented_html:
            print(f"   {p}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
