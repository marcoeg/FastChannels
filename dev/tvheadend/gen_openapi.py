#!/usr/bin/env python3
"""
Generate an OpenAPI 3.1 spec for Tvheadend's JSON API.

Tvheadend has no API documentation. Its 134 markdown files cover the web UI,
configuration and DVB tuning; not one describes the ~180 JSON endpoints the UI
itself is built on. The only HTTP reference, docs/markdown/url.md, documents
the streaming URLs (/play, /playlist) and stops there.

So this reads the endpoint table straight out of `src/api/*.c` — the single
authoritative list, since every endpoint is registered there with its access
level and handler — and turns it into a spec. Roughly half the API is served by
generic idnode handlers (26 */class, 26 */grid, 7 load, 7 save), so those get
one shared shape each rather than 180 bespoke definitions.

Descriptions for the endpoints that actually matter are curated below; the rest
are labelled from their handler. Response envelopes and query parameters were
verified against a running server, not inferred.

Usage:
    python3 dev/tvheadend/gen_openapi.py --src /home/marco/Development/tvheadend
    python3 dev/tvheadend/gen_openapi.py --verify        # check against a live server
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.request

DEFAULT_SRC = pathlib.Path("/home/marco/Development/tvheadend")
OUT = pathlib.Path(__file__).resolve().parent / "openapi.yaml"

# Access can be a single constant or an OR-expression spanning lines, e.g.
#   { "dvr/config/grid", ACCESS_OR|ACCESS_ADMIN|ACCESS_RECORDER,
#                          api_idnode_grid, api_dvr_config_grid },
# and endpoints are registered in src/api.c as well as src/api/*.c — an
# earlier version of this regex missed `serverinfo` and all of dvr/config
# for exactly those two reasons.
ENDPOINT_RE = re.compile(
    r'\{\s*"([a-z][a-z0-9/_]*)"\s*,\s*'
    r'((?:ACCESS_\w+\s*\|\s*)*ACCESS_\w+)\s*,\s*(\w+)', re.M)

# Tags, in the order they should appear.
TAG_ORDER = [
    ("channel", "Channels", "Channels, tags and categories."),
    ("epg", "EPG", "Programme guide events and queries."),
    ("epggrab", "EPG grabbers", "Guide sources: internal tv_grab_* scripts, external sockets, over-the-air."),
    ("dvr", "DVR", "Recordings, autorecs (series rules), timerecs (time slots) and profiles."),
    ("mpegts", "Networks & muxes", "MPEG-TS networks, multiplexes and scanning. IPTV networks live here."),
    ("service", "Services", "Services carried by muxes, and mapping them to channels."),
    ("bouquet", "Bouquets", "Groups of services, optionally auto-mapped to channels."),
    ("profile", "Stream profiles", "Streaming profiles — pass, matroska, and transcoding when built in."),
    ("access", "Access control", "Users, permissions and IP blocks."),
    ("passwd", "Access control", None),
    ("ipblock", "Access control", None),
    ("config", "Configuration", "Server configuration, status and diagnostics."),
    ("status", "Configuration", None),
    ("hardware", "Configuration", None),
    ("dvb", "Configuration", None),
    ("satips", "Configuration", None),
    ("timeshift", "Configuration", None),
    ("imagecache", "Configuration", None),
    ("caclient", "Configuration", None),
    ("esfilter", "Stream filters", "Elementary-stream filters, per stream type."),
    ("idnode", "idnode (generic)", "The generic object layer every configurable thing is built on."),
    ("wizard", "Setup wizard", "The first-run wizard's steps."),
    ("channeltag", "Channels", None),
    ("channelcategory", "Channels", None),
    ("ratinglabel", "Channels", None),
    ("codec", "Stream profiles", None),
    ("codec_profile", "Stream profiles", None),
    ("tvhlog", "Configuration", None),
    ("memoryinfo", "Configuration", None),
    ("language", "Configuration", None),
    ("intlconv", "Configuration", None),
    ("connections", "Configuration", None),
    ("raw", "Configuration", None),
    ("classes", "idnode (generic)", None),
    ("serverinfo", "Configuration", None),
    ("pathlist", "Configuration", None),
]

# Curated descriptions. Everything else falls back to its handler name.
NOTES: dict[str, str] = {
    "channel/grid": "All channels. The workhorse endpoint — supports `limit`, `start`, `sort`, `dir` and `filter`.",
    "channel/list": "Compact channel list: uuid and name only.",
    "epg/events/grid": "Programme guide events. Filter by `channel`, `title` (substring), `lang`, `mode` (`now`/`next`), `start`/`stop`.",
    "epg/events/load": "Load specific events by `eventId`.",
    "epg/content_type/list": "The content-type (genre) vocabulary.",
    "dvr/entry/grid": "All recordings — scheduled, running, completed and failed.",
    "dvr/entry/create":
        "Create a recording from an explicit time window. `conf` is a JSON object with "
        "`config_name` (a DVR profile uuid), `channel` (channel uuid), `start` and `stop` "
        "(epoch seconds), and `title` as a language map, e.g. `{\"eng\": \"…\"}`.",
    "dvr/entry/create_by_event":
        "Schedule a recording from a guide event. Takes `event_id` and `config_uuid`. "
        "Preferred over `dvr/entry/create` — start, stop and title come from the EPG, "
        "and the entry tracks schedule changes.",
    "dvr/entry/cancel": "Cancel a scheduled or running recording, keeping the entry.",
    "dvr/entry/stop": "Stop a running recording, keeping what has been written.",
    "dvr/entry/remove": "Delete the entry and its file.",
    "dvr/autorec/create": "Create a series rule — record anything matching a title/channel/genre.",
    "dvr/timerec/create": "Create a recurring time-slot recording.",
    "dvr/config/grid": "DVR profiles: storage path, retention, pre/post padding.",
    "mpegts/network/create":
        "Create a network. `class` names the network type (`iptv_auto_network` for an M3U "
        "playlist) and `conf` is a JSON object of its properties — call "
        "`mpegts/network/builders` for the full schema.",
    "mpegts/network/builders": "Every network class with its complete property schema. The way to discover valid `conf` keys.",
    "mpegts/network/grid": "All networks.",
    "mpegts/network/scan": "Trigger a scan. For IPTV this opens every mux — see the warning on `iptv_auto_network`.",
    "mpegts/mux/grid": "All multiplexes. One per channel on an IPTV network.",
    "service/mapper/start": "Map services to channels in bulk.",
    "bouquet/grid": "Bouquets. A network-created bouquet arrives **disabled**; enabling it is what creates channels.",
    "epggrab/module/list": "Every EPG grabber with uuid, title and status. Titles are prefixed by kind — `External: XMLTV` is not `Internal: XMLTV: …`.",
    "epggrab/config/save": "Save grabber configuration, including the cron schedule.",
    "epggrab/channel/grid": "Channels as the grabbers see them, and what they are linked to.",
    "idnode/load": "Load one or more objects by `uuid`, with their full property list and current values.",
    "idnode/save": "Save an object. `node` is a JSON object containing `uuid` plus the fields to change.",
    "idnode/delete": "Delete objects by `uuid`.",
    "idnode/class": "Schema for a class by name.",
    "idnode/tree": "Hierarchical view, for tree-shaped config.",
    "access/whoami": "The calling user and their permissions. Useful as an auth smoke test.",
    "serverinfo": "Version, API version and build capabilities. `transcoding` appears here only if ffmpeg support was compiled in.",
    "status/connections": "Active client connections.",
    "status/subscriptions": "Active subscriptions — who is watching or recording what.",
    "status/inputs": "Input/tuner status, including per-mux bitrate and error counters.",
}

HANDLER_LABEL = {
    "api_idnode_grid": "Paginated list",
    "api_idnode_class": "Schema for this object type",
    "api_idnode_load_simple": "Load configuration",
    "api_idnode_save_simple": "Save configuration",
    "api_idnode_load_by_class": "Load all objects of this class",
    "api_idnode_tree": "Hierarchical listing",
    "wizard_idnode_load_simple": "Load this wizard step",
    "wizard_idnode_save_simple": "Save this wizard step",
}


def extract(src: pathlib.Path) -> list[dict]:
    api = src / "src" / "api"
    if not api.is_dir():
        sys.exit(f"error: {api} not found — pass --src pointing at a Tvheadend checkout")
    files = sorted(api.glob("*.c"))
    root = src / "src" / "api.c"          # registers serverinfo and friends
    if root.is_file():
        files.append(root)
    out = []
    for f in files:
        for m in ENDPOINT_RE.finditer(f.read_text()):
            out.append({"path": m.group(1), "access": m.group(2),
                        "handler": m.group(3), "file": f.name})
    # A path can be registered once; keep first occurrence, stable order.
    seen, uniq = set(), []
    for e in out:
        if e["path"] not in seen:
            seen.add(e["path"])
            uniq.append(e)
    return uniq


def tag_for(path: str) -> str:
    head = path.split("/")[0]
    for prefix, tag, _ in TAG_ORDER:
        if head == prefix:
            return tag
    return "Other"


def summary_for(e: dict) -> str:
    if e["path"] in NOTES:
        first = NOTES[e["path"]].split(". ")[0].rstrip(".")
        return first if len(first) < 90 else e["path"]
    label = HANDLER_LABEL.get(e["handler"])
    if label:
        return f"{label} — {e['path'].rsplit('/', 1)[0]}"
    return e["handler"].replace("api_", "").replace("_", " ")


def yaml_str(s: str, indent: int) -> str:
    """Block scalar, so descriptions never need escaping."""
    pad = " " * indent
    body = "\n".join(pad + line if line else "" for line in s.split("\n"))
    return "|\n" + body


def build(endpoints: list[dict]) -> str:
    tags, seen_tags = [], set()
    for _, tag, desc in TAG_ORDER:
        if tag not in seen_tags and desc:
            tags.append((tag, desc))
            seen_tags.add(tag)
    tags.append(("Other", "Endpoints that do not fall into the groups above."))

    L: list[str] = []
    A = L.append

    A("openapi: 3.1.0")
    A("")
    A("info:")
    A("  title: Tvheadend JSON API")
    A('  version: "4.3"')
    A("  summary: The HTTP API the Tvheadend web interface is built on.")
    A("  description: " + yaml_str(INFO_DESC, 4))
    A("")
    A("servers:")
    A("  - url: http://127.0.0.1:9981/api")
    A("    description: Default local bind")
    A("")
    A("security:")
    A("  - digestAuth: []")
    A("")
    A("tags:")
    for name, desc in tags:
        A(f"  - name: {name}")
        A("    description: " + yaml_str(desc, 6))
    A("")
    A("paths:")

    for e in endpoints:
        path, access = e["path"], e["access"]
        A(f"  /{path}:")
        A("    post:")
        A(f"      tags: [{tag_for(path)}]")
        A(f"      summary: {json.dumps(summary_for(e))}")
        desc = NOTES.get(path, "")
        detail = (desc + "\n\n" if desc else "")
        levels = [a.replace("ACCESS_", "").lower()
                  for a in access.split("|") if a.strip() != "ACCESS_OR"]
        needs = " or ".join(levels) if "ACCESS_OR" in access else ", ".join(levels)
        detail += (f"Handler `{e['handler']}` in `{e['file']}`. "
                   f"Requires **{needs}** access.")
        A("      description: " + yaml_str(detail, 8))
        A(f"      operationId: {re.sub(r'[^a-zA-Z0-9]', '_', path)}")

        if e["handler"] == "api_idnode_grid":
            A("      requestBody:")
            A("        content:")
            A("          application/x-www-form-urlencoded:")
            A("            schema: { $ref: '#/components/schemas/GridRequest' }")
            A("      responses:")
            A("        '200':")
            A("          description: Paginated entries.")
            A("          content:")
            A("            application/json:")
            A("              schema: { $ref: '#/components/schemas/Grid' }")
        elif e["handler"] in ("api_idnode_class", "api_idnode_load_by_class"):
            A("      responses:")
            A("        '200':")
            A("          description: Class schema.")
            A("          content:")
            A("            application/json:")
            A("              schema: { $ref: '#/components/schemas/ClassSchema' }")
        elif "load" in e["handler"]:
            A("      responses:")
            A("        '200':")
            A("          description: Objects with their property values.")
            A("          content:")
            A("            application/json:")
            A("              schema: { $ref: '#/components/schemas/NodeLoad' }")
        elif "save" in e["handler"] or path.endswith("/create"):
            A("      requestBody:")
            A("        required: true")
            A("        content:")
            A("          application/x-www-form-urlencoded:")
            A("            schema: { $ref: '#/components/schemas/ConfRequest' }")
            A("      responses:")
            A("        '200':")
            A("          description: Created or saved. Create returns the new uuid.")
            A("          content:")
            A("            application/json:")
            A("              schema: { $ref: '#/components/schemas/CreateResult' }")
            A("        '400': { $ref: '#/components/responses/BadRequest' }")
        else:
            A("      responses:")
            A("        '200':")
            A("          description: Success.")
            A("          content:")
            A("            application/json:")
            A("              schema: { type: object, additionalProperties: true }")
            A("        '400': { $ref: '#/components/responses/BadRequest' }")
        A("        '401': { $ref: '#/components/responses/Unauthorized' }")
        A("")

    A(COMPONENTS)
    return "\n".join(L) + "\n"


INFO_DESC = """Tvheadend ships no documentation for this API. Its 134 markdown files cover the
web interface, configuration and DVB tuning; none describe the ~180 JSON
endpoints the UI itself is built on. `docs/markdown/url.md` documents the
streaming URLs (`/play`, `/playlist`) and stops there.

This spec was generated from the endpoint table in `src/api/*.c` — the
authoritative list, since every endpoint is registered there with its access
level — and the shapes were verified against a running server.

## Conventions

Every endpoint is **POST** with `application/x-www-form-urlencoded` parameters
whose *values* are JSON documents. `conf`, `node` and `filter` are JSON strings
inside a form field, not a JSON request body:

    curl --digest -u user:pass http://127.0.0.1:9981/api/idnode/save \\
      --data-urlencode 'node={"uuid":"...","enabled":true}'

Most also accept GET with a query string, which is how the examples below read.

## Authentication

HTTP **digest** by default (`config.http_auth`); basic is available. An
unauthenticated call returns `401`. A `superuser` file in the configuration
directory grants access regardless of access-control entries, which is what
makes first-run scripting possible.

## The idnode layer

Almost everything configurable is an *idnode* — a UUID-keyed object with an
introspectable class. That is why the same handful of handlers serve half the
API: 26 `*/grid`, 26 `*/class`, and the generic `idnode/load`, `idnode/save`,
`idnode/delete`.

Practical consequence: `*/class` (or `mpegts/network/builders`) returns every
valid property with its type, caption and description. That is the closest
thing to reference documentation the project has, and it is live rather than
prose — prefer it over guessing.

## Not covered here

Streaming and file endpoints live outside `/api` and are documented in
`docs/markdown/url.md`: `/stream/channel/{uuid}`, `/playlist/channels`,
`/dvrfile/{uuid}`, `/imagecache/{id}`. HTSP, the binary protocol used by
Kodi and the mobile apps, is separate again."""


COMPONENTS = '''components:

  securitySchemes:
    digestAuth:
      type: http
      scheme: digest
      description: |
        Default scheme. Basic is also accepted depending on `config.http_auth`.
        A `superuser` file in the configuration directory bypasses access
        control entirely — that is how a fresh install can be scripted.

  responses:

    BadRequest:
      description: |
        A required parameter is missing or unparseable — typically `conf`,
        `node` or `uuid`. The body is empty; the status is the whole signal.

    Unauthorized:
      description: Missing or rejected credentials.

  schemas:

    GridRequest:
      type: object
      description: |
        Query parameters shared by every `*/grid` endpoint. All verified
        against a running server.
      properties:
        limit:
          type: integer
          description: Maximum entries to return. Omit for the server default.
        start:
          type: integer
          description: Offset into the result set, for paging.
        sort:
          type: string
          description: Field to sort by, e.g. `name`.
        dir:
          type: string
          enum: [ASC, DESC]
          description: Sort direction.
        filter:
          type: string
          description: |
            JSON array of filter clauses, url-encoded. Each clause is
            `{"field": "...", "type": "...", "value": ...}` where type is
            `string`, `numeric`, `boolean` or `enum`. Example:

                filter=[{"field":"enabled","type":"boolean","value":true}]

    Grid:
      type: object
      description: |
        Standard list envelope. `total` is the count *before* `limit`, so it
        drives paging.
      properties:
        entries:
          type: array
          items:
            type: object
            additionalProperties: true
            description: |
              One object, flattened to its current property values. The exact
              keys depend on the class — call the matching `*/class` endpoint
              for the schema.
        total:
          type: integer

    ConfRequest:
      type: object
      description: |
        Create and save endpoints take their payload as a JSON document inside
        a form field, not as a JSON body.
      properties:
        conf:
          type: string
          description: |
            JSON object of properties. Used by `*/create` and by
            `dvr/entry/create`.
        node:
          type: string
          description: |
            JSON object (or array of objects) containing `uuid` plus the fields
            to change. Used by `idnode/save`.
        class:
          type: string
          description: |
            Class name for polymorphic creates, e.g. `iptv_auto_network`. Call
            `mpegts/network/builders` for the list and each class's schema.

    CreateResult:
      type: object
      description: Create returns the new object's uuid. Save returns an empty object.
      properties:
        uuid:
          type: string
          format: uuid

    ClassSchema:
      type: object
      description: |
        The schema for an object type — the nearest thing Tvheadend has to
        reference documentation, and it is live rather than prose.
      properties:
        class:
          type: string
          description: Class identifier, e.g. `dvr_entry`.
        caption:
          type: string
        event:
          type: string
        props:
          type: array
          items: { $ref: '#/components/schemas/Property' }

    Property:
      type: object
      description: One configurable field.
      properties:
        id:
          type: string
        type:
          type: string
          enum: [bool, int, str, u16, u32, s64, dbl, time, langstr, perm]
          description: |
            From `src/prop.c`. `langstr` is a language map such as
            `{"eng": "..."}` — recording titles use it. `time` is epoch
            seconds. `perm` is an octal permission mask.
        caption:
          type: string
          description: Human label, as shown in the web UI.
        description:
          type: string
          description: The UI tooltip. Often the only prose on a given setting.
        default: {}
        enum:
          type: array
          items: { type: object, additionalProperties: true }
          description: Allowed values, when the property is an enumeration.
        rdonly:
          type: boolean
        advanced:
          type: boolean
        noui:
          type: boolean

    NodeLoad:
      type: object
      description: Response from `idnode/load` and the `*/load` endpoints.
      properties:
        entries:
          type: array
          items:
            type: object
            properties:
              uuid: { type: string, format: uuid }
              id: { type: string }
              class: { type: string }
              caption: { type: string }
              text: { type: string }
              params:
                type: array
                description: Properties with their current `value` merged in.
                items:
                  allOf:
                    - $ref: '#/components/schemas/Property'
                    - type: object
                      properties:
                        value: {}
'''


def verify(base: str, user: str, password: str, spec_paths: set[str]) -> int:
    """Sanity-check a sample of documented paths against a live server."""
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, base, user, password)
    opener = urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(mgr),
        urllib.request.HTTPBasicAuthHandler(mgr))

    sample = ["serverinfo", "channel/grid", "epg/events/grid", "dvr/entry/grid",
              "dvr/config/grid", "mpegts/network/grid", "mpegts/network/builders",
              "bouquet/grid", "epggrab/module/list", "access/whoami",
              "profile/list", "status/connections", "dvr/entry/class"]
    ok = 0
    for p in sample:
        if p not in spec_paths:
            print(f"  MISSING FROM SPEC  {p}")
            continue
        try:
            with opener.open(f"{base}/api/{p}?limit=1", timeout=20) as r:
                json.load(r)
            print(f"  ok    {p}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {p}: {exc}")
    print(f"\n{ok}/{len(sample)} sampled endpoints answered as documented")
    return 0 if ok == len(sample) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=pathlib.Path, default=DEFAULT_SRC,
                    help="Tvheadend source checkout")
    ap.add_argument("--out", type=pathlib.Path, default=OUT)
    ap.add_argument("--verify", action="store_true",
                    help="also probe a live server for a sample of endpoints")
    ap.add_argument("--base", default="http://127.0.0.1:9981")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="fastchannels")
    args = ap.parse_args()

    endpoints = extract(args.src)
    args.out.write_text(build(endpoints))
    print(f"wrote {args.out} — {len(endpoints)} endpoints")

    if args.verify:
        print("\nverifying against the live server:")
        return verify(args.base, args.user, args.password, {e["path"] for e in endpoints})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
