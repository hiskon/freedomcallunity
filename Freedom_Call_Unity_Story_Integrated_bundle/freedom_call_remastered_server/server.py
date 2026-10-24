#!/usr/bin/env python3
"""Freedom Call Remastered WebSocket room and co-op naval/fort server.

Development/LAN prototype. Add authentication, WSS/TLS, persistence and monitoring
before a public internet deployment.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

PORT = int(os.environ.get("PORT", "8080"))
MAX_PLAYERS = 4
MAX_NAME_LENGTH = 24
MESSAGE_RATE_SECONDS = 0.28
ALLOWED_SHIPS = {"aigle", "liberte", "etoile"}
ALLOWED_FORTS = {"channel_fort", "saint_malo", "royal_supply"}
rooms: dict[str, "Room"] = {}


def make_naval_state() -> dict[str, Any]:
    return {
        "currentFortId": "channel_fort",
        "hull": 100,
        "crewMorale": 72,
        "captureProgress": 0,
        "francs": 50,
        "forts": {
            "channel_fort": {"defense": 100, "captured": False, "nameKey": "fortA"},
            "saint_malo": {"defense": 115, "captured": False, "nameKey": "fortB"},
            "royal_supply": {"defense": 140, "captured": False, "nameKey": "fortC"},
        },
        "capturedForts": [],
    }


def clean_name(value: Any) -> str:
    name = "".join(ch for ch in str(value or "Assassin") if ch >= " " and ch not in "<>\x7f")
    return name.strip()[:MAX_NAME_LENGTH] or "Assassin"


def clean_code(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isascii() and ch.isalnum())[:8]


def new_room_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if code not in rooms:
            return code


@dataclass
class Player:
    id: str
    websocket: Any
    name: str = "Assassin"
    ready: bool = False
    room_code: str = ""
    ship_id: str = "liberte"
    last_action_at: float = 0.0


@dataclass
class Room:
    id: str
    code: str
    host_id: str
    phase: str = "lobby"
    mission_id: str | None = None
    players: dict[str, Player] = field(default_factory=dict)
    naval_state: dict[str, Any] = field(default_factory=make_naval_state)


def json_message(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


async def send(player: Player, message: dict[str, Any]) -> None:
    try:
        await player.websocket.send(json_message(message))
    except (ConnectionClosed, RuntimeError):
        pass


async def fail(player: Player, message: str, code: str = "BAD_REQUEST") -> None:
    await send(player, {"type": "error", "code": code, "message": message})


def room_state(room: Room) -> dict[str, Any]:
    return {
        "type": "roomState",
        "roomId": room.id,
        "roomCode": room.code,
        "hostId": room.host_id,
        "phase": room.phase,
        "missionId": room.mission_id,
        "players": [
            {"id": p.id, "name": p.name, "ready": p.ready, "shipId": p.ship_id}
            for p in room.players.values()
        ],
        "navalState": room.naval_state,
    }


async def broadcast(room: Room, message: dict[str, Any]) -> None:
    await asyncio.gather(*(send(p, message) for p in list(room.players.values())), return_exceptions=True)


async def broadcast_state(room: Room) -> None:
    await broadcast(room, room_state(room))


async def broadcast_notice(room: Room, message: str) -> None:
    await broadcast(room, {"type": "notice", "message": message})


async def remove_from_room(player: Player) -> None:
    code = player.room_code
    if not code:
        return
    player.room_code = ""
    room = rooms.get(code)
    if not room:
        return
    room.players.pop(player.id, None)
    if not room.players:
        rooms.pop(code, None)
        return
    if room.host_id == player.id:
        room.host_id = next(iter(room.players))
        room.players[room.host_id].ready = False
    await broadcast_notice(room, f"{player.name} left the room.")
    await broadcast_state(room)


async def add_player_to_room(player: Player, room: Room, name: Any) -> None:
    if player.room_code:
        await remove_from_room(player)
    if len(room.players) >= MAX_PLAYERS:
        await fail(player, "Room is full.", "ROOM_FULL")
        return
    if room.phase != "lobby":
        await fail(player, "This operation has already started. Join another room.", "ROOM_STARTED")
        return
    player.name = clean_name(name)
    player.ready = False
    player.room_code = room.code
    player.ship_id = "liberte"
    room.players[player.id] = player
    await send(player, {"type": "notice", "message": f"Joined room {room.code}."})
    await broadcast_state(room)


async def handle_naval_action(player: Player, room: Room, message: dict[str, Any]) -> None:
    if room.phase != "naval_ops":
        await fail(player, "Start the co-op operation from the host first.", "MISSION_NOT_STARTED")
        return
    state = room.naval_state
    action = str(message.get("action", ""))
    ship_id = message.get("shipId") if message.get("shipId") in ALLOWED_SHIPS else "liberte"
    fort_id = message.get("fortId") if message.get("fortId") in ALLOWED_FORTS else state["currentFortId"]

    if action == "selectFort":
        state["currentFortId"] = fort_id
        state["captureProgress"] = 0
        player.ship_id = ship_id
        await broadcast_state(room)
        return
    if action not in {"broadside", "sabotage", "rally", "repair", "boarding"}:
        await fail(player, "Unsupported naval action.", "INVALID_ACTION")
        return
    now = time.monotonic()
    if now - player.last_action_at < MESSAGE_RATE_SECONDS:
        await fail(player, "Please wait before issuing another order.", "RATE_LIMITED")
        return
    player.last_action_at = now
    player.ship_id = ship_id
    fort = state["forts"].get(state["currentFortId"])
    if not fort:
        await fail(player, "Target fort not found.", "INVALID_FORT")
        return
    cannon_power = {"aigle": 14, "liberte": 20, "etoile": 11}[ship_id]

    if action == "broadside":
        if fort["captured"]:
            await fail(player, "This fort has already been captured.", "ALREADY_CAPTURED")
            return
        damage = max(10, round(cannon_power * (0.65 + state["crewMorale"] / 200)))
        fort["defense"] = max(0, fort["defense"] - damage)
        state["hull"] = max(0, state["hull"] - 4)
        state["crewMorale"] = max(0, state["crewMorale"] - 3)
    elif action == "sabotage":
        if fort["captured"]:
            await fail(player, "This fort has already been captured.", "ALREADY_CAPTURED")
            return
        fort["defense"] = max(0, fort["defense"] - 14)
        state["crewMorale"] = max(0, state["crewMorale"] - 4)
    elif action == "rally":
        state["crewMorale"] = min(100, state["crewMorale"] + 18)
    elif action == "repair":
        if state["francs"] < 10:
            await fail(player, "The crew needs at least 10 francs to repair the hull.", "NOT_ENOUGH_FRANCS")
            return
        state["francs"] -= 10
        state["hull"] = min(100, state["hull"] + 18)
    elif action == "boarding":
        if fort["captured"]:
            await fail(player, "This fort has already been captured.", "ALREADY_CAPTURED")
            return
        if fort["defense"] > 35:
            await fail(player, "Weaken the fort defence before boarding.", "FORT_TOO_STRONG")
            return
        if state["hull"] <= 0 or state["crewMorale"] < 12:
            await fail(player, "Hull or crew morale is too low to board.", "CREW_UNREADY")
            return
        state["captureProgress"] = min(100, state["captureProgress"] + max(18, round(22 + state["crewMorale"] / 12)))
        state["crewMorale"] = max(0, state["crewMorale"] - 9)
        state["hull"] = max(0, state["hull"] - 3)
        if state["captureProgress"] >= 100:
            fort["captured"] = True
            if state["currentFortId"] not in state["capturedForts"]:
                state["capturedForts"].append(state["currentFortId"])
                state["francs"] += 180
                await broadcast_notice(room, f"Fort captured by the crew: {fort['nameKey']}. Team reward +180 francs, +140 XP.")
    await broadcast_state(room)


async def handle_message(player: Player, message: dict[str, Any]) -> None:
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        await fail(player, "Missing message type.")
        return
    kind = message["type"]

    if kind == "createRoom":
        await remove_from_room(player)
        code = new_room_code()
        room = Room(id=str(uuid.uuid4()), code=code, host_id=player.id)
        rooms[code] = room
        await add_player_to_room(player, room, message.get("name"))
        return
    if kind == "joinRoom":
        code = clean_code(message.get("roomCode"))
        room = rooms.get(code)
        if room is None:
            await fail(player, "Room not found. Check the code or create a new room.", "ROOM_NOT_FOUND")
            return
        await add_player_to_room(player, room, message.get("name"))
        return
    if kind == "setReady":
        room = rooms.get(player.room_code)
        if room is None or player.id not in room.players:
            await fail(player, "Join a room first.", "NOT_IN_ROOM")
            return
        if room.phase != "lobby":
            await fail(player, "Ready state is locked after the operation begins.", "MISSION_STARTED")
            return
        player.ready = bool(message.get("ready"))
        await broadcast_state(room)
        return
    if kind == "startMission":
        room = rooms.get(player.room_code)
        if room is None or player.id not in room.players:
            await fail(player, "Join a room first.", "NOT_IN_ROOM")
            return
        if room.host_id != player.id:
            await fail(player, "Only the host can start the operation.", "HOST_ONLY")
            return
        if not all(p.ready for p in room.players.values()):
            await fail(player, "Every player must be ready.", "PLAYERS_NOT_READY")
            return
        room.phase = "naval_ops"
        room.mission_id = "cooperative_naval_assault"
        await broadcast_notice(room, "Co-op naval assault started. All fort attacks now share authoritative state.")
        await broadcast_state(room)
        return
    if kind == "navalAction":
        room = rooms.get(player.room_code)
        if room is None or player.id not in room.players:
            await fail(player, "Join a room first.", "NOT_IN_ROOM")
            return
        await handle_naval_action(player, room, message)
        return
    if kind == "leaveRoom":
        await remove_from_room(player)
        return
    if kind == "ping":
        await send(player, {"type": "pong", "at": int(time.time() * 1000)})
        return
    await fail(player, "Unknown message type.", "UNKNOWN_MESSAGE")


async def handler(websocket: Any) -> None:
    player = Player(id=str(uuid.uuid4()), websocket=websocket)
    await send(player, {"type": "welcome", "playerId": player.id, "protocolVersion": 1, "serverTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    try:
        async for raw in websocket:
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > 8192:
                await fail(player, "Message too large.", "MESSAGE_TOO_LARGE")
                continue
            try:
                message = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                await fail(player, "Invalid JSON.", "INVALID_JSON")
                continue
            await handle_message(player, message)
    except ConnectionClosed:
        pass
    finally:
        await remove_from_room(player)


async def main() -> None:
    print(f"Freedom Call Remastered co-op server listening on ws://0.0.0.0:{PORT}")
    print("Development/LAN prototype only. Add WSS/TLS, authentication and persistence before public deployment.")
    async with websockets.serve(handler, "0.0.0.0", PORT, max_size=8192, compression=None, ping_interval=20):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Co-op server stopped.")
