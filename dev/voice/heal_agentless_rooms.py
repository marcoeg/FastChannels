#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyjwt"]
# ///
"""Delete LiveKit rooms that have participants but no agent.

Why: when the voice worker restarts (daily 04:30 recycle, manual restart),
its in-flight jobs die but the rooms survive — and LiveKit does NOT
re-dispatch an agent to a pre-existing room. A permanently-connected client
like the ESP32 satellite (room "speaker") then sits in an agent-less room
forever. Deleting the room disconnects the client, whose reconnect loop
recreates it seconds later WITH a fresh agent dispatch.

Run via ExecStartPost of voice-agent.service (sleeps first so the freshly
restarted worker is registered before rooms are healed). Rooms younger than
AGE_MIN_S are left alone — their agent may legitimately still be joining.
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

import jwt

HERE = Path(__file__).parent
env = dict(
    line.split("=", 1)
    for line in (HERE / ".env").read_text().splitlines()
    if line and not line.startswith("#") and "=" in line
)

URL = "http://127.0.0.1:7880"
AGE_MIN_S = 45

now = int(time.time())


def mint(video: dict) -> str:
    return jwt.encode(
        {"iss": env["LIVEKIT_API_KEY"].strip(), "sub": "room-healer",
         "nbf": now - 60, "exp": now + 300, "video": video},
        env["LIVEKIT_API_SECRET"].strip(), algorithm="HS256")


# roomAdmin is room-scoped: ListParticipants needs a token naming the room,
# while ListRooms/DeleteRoom work off roomList/roomCreate.
base_token = mint({"roomCreate": True, "roomList": True})


def twirp(method: str, body: dict, token: str) -> dict:
    req = urllib.request.Request(
        f"{URL}/twirp/livekit.RoomService/{method}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


healed = []
for room in twirp("ListRooms", {}, base_token).get("rooms", []):
    name = room["name"]
    created = int(room.get("creation_time") or room.get("creationTime") or 0)
    if created and now - created < AGE_MIN_S:
        continue
    room_token = mint({"roomAdmin": True, "room": name})
    participants = twirp("ListParticipants", {"room": name},
                         room_token).get("participants", [])
    if not participants:
        continue  # empty rooms expire on their own
    if any(p.get("kind") == "AGENT" for p in participants):
        continue
    twirp("DeleteRoom", {"room": name}, base_token)
    healed.append(name)

print(f"healed {healed}" if healed else "no agent-less rooms")
sys.exit(0)
