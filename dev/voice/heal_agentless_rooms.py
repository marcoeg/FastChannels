#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyjwt"]
# ///
"""Delete LiveKit rooms that have participants but no (live) agent.

Why: when the voice worker restarts (daily 04:30 recycle, manual restart),
its in-flight jobs die but the rooms survive — and LiveKit does NOT
re-dispatch an agent to a pre-existing room. A permanently-connected client
like the ESP32 satellite (room "speaker") then sits in an agent-less room
forever. Deleting the room disconnects the client, whose reconnect loop
recreates it seconds later WITH a fresh agent dispatch.

Two modes:

  heal_agentless_rooms.py                  periodic (voice-room-healer.timer):
      delete rooms with participants and no AGENT participant at all; rooms
      younger than AGE_MIN_S are left alone (their agent may still be joining).

  heal_agentless_rooms.py --after-restart  ExecStartPost of voice-agent.service:
      the dead worker's agent lingers in the room for 20-60 s (server
      departure timeout), so "has an agent" can't be trusted right after a
      restart. Instead: once the new worker answers on its health port, any
      room whose clients joined BEFORE this restart and whose agents all
      joined before it too is orphaned — delete it at once. Polls every few
      seconds until no such room is left (or TIMEOUT_S), so the satellite is
      back within seconds instead of after a fixed 75 s sleep.
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


WORKER_HEALTH = "http://127.0.0.1:8081/worker"
POLL_S = 3
TIMEOUT_S = 120


def participants(name: str) -> list[dict]:
    return twirp("ListParticipants", {"room": name},
                 mint({"roomAdmin": True, "room": name})).get("participants", [])


def joined(p: dict) -> int:
    return int(p.get("joined_at") or p.get("joinedAt") or 0)


def periodic() -> list[str]:
    healed = []
    for room in twirp("ListRooms", {}, base_token).get("rooms", []):
        name = room["name"]
        created = int(room.get("creation_time") or room.get("creationTime") or 0)
        if created and now - created < AGE_MIN_S:
            continue
        ps = participants(name)
        if not ps:
            continue  # empty rooms expire on their own
        if any(p.get("kind") == "AGENT" for p in ps):
            continue
        twirp("DeleteRoom", {"room": name}, base_token)
        healed.append(name)
    return healed


def worker_up() -> bool:
    try:
        with urllib.request.urlopen(WORKER_HEALTH, timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def after_restart() -> list[str]:
    t0 = now  # this runs as ExecStartPost: the old worker is already gone
    deadline = time.time() + TIMEOUT_S
    while not worker_up():
        if time.time() > deadline:
            print("worker never came up; nothing healed")
            return []
        time.sleep(1)
    time.sleep(2)  # the health port opens ~2 s before LiveKit registration
    healed = []
    while time.time() < deadline:
        pending = 0
        for room in twirp("ListRooms", {}, base_token).get("rooms", []):
            name = room["name"]
            ps = participants(name)
            clients = [p for p in ps if p.get("kind") != "AGENT"]
            if not clients or name in healed:
                continue
            if any(joined(p) >= t0 for p in clients):
                continue  # rejoined after the restart: dispatched normally
            if any(p.get("kind") == "AGENT" and joined(p) >= t0 for p in ps):
                continue  # already has a new agent
            twirp("DeleteRoom", {"room": name}, base_token)
            healed.append(name)
            pending += 1
        if not pending:
            break
        time.sleep(POLL_S)
    return healed


healed = after_restart() if "--after-restart" in sys.argv else periodic()
print(time.strftime("%F %T"), f"healed {healed}" if healed else "no agent-less rooms")
sys.exit(0)
