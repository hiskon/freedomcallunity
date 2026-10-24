'use strict';

/**
 * Freedom Call: Unity Remastered - small authoritative WebSocket co-op server.
 * Scope: room/lobby state + cooperative naval/fort assault prototype.
 * Before public deployment add authentication, TLS termination, persistence,
 * moderation, metrics, origin allow-listing and stronger abuse controls.
 */
const crypto = require('node:crypto');
const { WebSocketServer, WebSocket } = require('ws');

const PORT = Number(process.env.PORT || 8080);
const MAX_PLAYERS = 4;
const MAX_NAME_LENGTH = 24;
const MESSAGE_RATE_MS = 280;
const rooms = new Map();
const allowedShips = new Set(['aigle', 'liberte', 'etoile']);
const allowedForts = new Set(['channel_fort', 'saint_malo', 'royal_supply']);

function makeNavalState() {
  return {
    currentFortId: 'channel_fort',
    hull: 100,
    crewMorale: 72,
    captureProgress: 0,
    francs: 50,
    forts: {
      channel_fort: { defense: 100, captured: false, nameKey: 'fortA' },
      saint_malo: { defense: 115, captured: false, nameKey: 'fortB' },
      royal_supply: { defense: 140, captured: false, nameKey: 'fortC' },
    },
    capturedForts: [],
  };
}

function send(ws, value) {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(value));
}
function fail(ws, message, code = 'BAD_REQUEST') {
  send(ws, { type: 'error', code, message });
}
function cleanName(value) {
  const name = String(value || 'Assassin').replace(/[<>\u0000-\u001f]/g, '').trim().slice(0, MAX_NAME_LENGTH);
  return name || 'Assassin';
}
function roomPlayers(room) {
  return [...room.players.values()].map(player => ({
    id: player.id,
    name: player.name,
    ready: player.ready,
    shipId: player.shipId,
  }));
}
function broadcastRoom(room) {
  const message = {
    type: 'roomState',
    roomId: room.id,
    roomCode: room.code,
    hostId: room.hostId,
    phase: room.phase,
    missionId: room.missionId,
    players: roomPlayers(room),
    navalState: room.navalState,
  };
  for (const player of room.players.values()) send(player.ws, message);
}
function broadcastNotice(room, message) {
  for (const player of room.players.values()) send(player.ws, { type: 'notice', message });
}
function makeRoomCode() {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let code;
  do {
    code = Array.from(crypto.randomBytes(6), byte => alphabet[byte % alphabet.length]).join('');
  } while (rooms.has(code));
  return code;
}
function removeFromRoom(ws) {
  const player = ws.fc;
  if (!player || !player.roomCode) return;
  const room = rooms.get(player.roomCode);
  player.roomCode = '';
  if (!room) return;
  room.players.delete(player.id);
  if (!room.players.size) {
    rooms.delete(room.code);
    return;
  }
  if (room.hostId === player.id) {
    room.hostId = room.players.keys().next().value;
    const newHost = room.players.get(room.hostId);
    if (newHost) newHost.ready = false;
  }
  broadcastNotice(room, `${player.name} left the room.`);
  broadcastRoom(room);
}
function joinRoom(ws, room, name) {
  if (ws.fc.roomCode) removeFromRoom(ws);
  if (room.players.size >= MAX_PLAYERS) return fail(ws, 'Room is full.', 'ROOM_FULL');
  if (room.phase !== 'lobby') return fail(ws, 'This operation has already started. Join another room.', 'ROOM_STARTED');
  const player = ws.fc;
  player.name = cleanName(name || player.name);
  player.ready = false;
  player.roomCode = room.code;
  player.shipId = 'liberte';
  room.players.set(player.id, player);
  send(ws, { type: 'notice', message: `Joined room ${room.code}.` });
  broadcastRoom(room);
}
function navalAction(ws, room, msg) {
  if (room.phase !== 'naval_ops') return fail(ws, 'Start the co-op operation from the host first.', 'MISSION_NOT_STARTED');
  const n = room.navalState;
  const action = String(msg.action || '');
  const shipId = allowedShips.has(msg.shipId) ? msg.shipId : 'liberte';
  const fortId = allowedForts.has(msg.fortId) ? msg.fortId : n.currentFortId;
  if (action === 'selectFort') {
    n.currentFortId = fortId;
    n.captureProgress = 0;
    ws.fc.shipId = shipId;
    broadcastRoom(room);
    return;
  }
  if (!['broadside', 'sabotage', 'rally', 'repair', 'boarding'].includes(action)) {
    return fail(ws, 'Unsupported naval action.', 'INVALID_ACTION');
  }
  const now = Date.now();
  if (now - ws.fc.lastActionAt < MESSAGE_RATE_MS) return fail(ws, 'Please wait before issuing another order.', 'RATE_LIMITED');
  ws.fc.lastActionAt = now;
  ws.fc.shipId = shipId;
  const fort = n.forts[n.currentFortId];
  if (!fort) return fail(ws, 'Target fort not found.', 'INVALID_FORT');
  const cannonPower = { aigle: 14, liberte: 20, etoile: 11 }[shipId];

  switch (action) {
    case 'broadside':
      if (fort.captured) return fail(ws, 'This fort has already been captured.', 'ALREADY_CAPTURED');
      fort.defense = Math.max(0, fort.defense - Math.max(10, Math.round(cannonPower * (0.65 + n.crewMorale / 200))));
      n.hull = Math.max(0, n.hull - 4);
      n.crewMorale = Math.max(0, n.crewMorale - 3);
      break;
    case 'sabotage':
      if (fort.captured) return fail(ws, 'This fort has already been captured.', 'ALREADY_CAPTURED');
      fort.defense = Math.max(0, fort.defense - 14);
      n.crewMorale = Math.max(0, n.crewMorale - 4);
      break;
    case 'rally':
      n.crewMorale = Math.min(100, n.crewMorale + 18);
      break;
    case 'repair':
      if (n.francs < 10) return fail(ws, 'The crew needs at least 10 francs to repair the hull.', 'NOT_ENOUGH_FRANCS');
      n.francs -= 10;
      n.hull = Math.min(100, n.hull + 18);
      break;
    case 'boarding':
      if (fort.captured) return fail(ws, 'This fort has already been captured.', 'ALREADY_CAPTURED');
      if (fort.defense > 35) return fail(ws, 'Weaken the fort defence before boarding.', 'FORT_TOO_STRONG');
      if (n.hull <= 0 || n.crewMorale < 12) return fail(ws, 'Hull or crew morale is too low to board.', 'CREW_UNREADY');
      n.captureProgress = Math.min(100, n.captureProgress + Math.max(18, Math.round(22 + n.crewMorale / 12)));
      n.crewMorale = Math.max(0, n.crewMorale - 9);
      n.hull = Math.max(0, n.hull - 3);
      if (n.captureProgress >= 100) {
        fort.captured = true;
        if (!n.capturedForts.includes(n.currentFortId)) n.capturedForts.push(n.currentFortId);
        n.francs += 180;
        broadcastNotice(room, `Fort captured by the crew: ${fort.nameKey}. Team reward +180 francs, +140 XP.`);
      }
      break;
  }
  broadcastRoom(room);
}

const wss = new WebSocketServer({ port: PORT, maxPayload: 8192, perMessageDeflate: false });
wss.on('connection', (ws, req) => {
  const playerId = crypto.randomUUID();
  ws.fc = { id: playerId, name: 'Assassin', ready: false, roomCode: '', shipId: 'liberte', lastActionAt: 0, connectedAt: Date.now() };
  send(ws, { type: 'welcome', playerId, protocolVersion: 1, serverTime: new Date().toISOString() });

  ws.on('message', raw => {
    if (raw.length > 8192) return fail(ws, 'Message too large.', 'MESSAGE_TOO_LARGE');
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch (_) { return fail(ws, 'Invalid JSON.', 'INVALID_JSON'); }
    if (!msg || typeof msg.type !== 'string') return fail(ws, 'Missing message type.');

    switch (msg.type) {
      case 'createRoom': {
        removeFromRoom(ws);
        const code = makeRoomCode();
        const room = { id: crypto.randomUUID(), code, hostId: ws.fc.id, phase: 'lobby', missionId: null, players: new Map(), navalState: makeNavalState(), createdAt: Date.now() };
        rooms.set(code, room);
        joinRoom(ws, room, msg.name);
        break;
      }
      case 'joinRoom': {
        const code = String(msg.roomCode || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 8);
        const room = rooms.get(code);
        if (!room) return fail(ws, 'Room not found. Check the code or create a new room.', 'ROOM_NOT_FOUND');
        joinRoom(ws, room, msg.name);
        break;
      }
      case 'setReady': {
        const room = rooms.get(ws.fc.roomCode);
        if (!room || !room.players.has(ws.fc.id)) return fail(ws, 'Join a room first.', 'NOT_IN_ROOM');
        if (room.phase !== 'lobby') return fail(ws, 'Ready state is locked after the operation begins.', 'MISSION_STARTED');
        ws.fc.ready = Boolean(msg.ready);
        broadcastRoom(room);
        break;
      }
      case 'startMission': {
        const room = rooms.get(ws.fc.roomCode);
        if (!room || !room.players.has(ws.fc.id)) return fail(ws, 'Join a room first.', 'NOT_IN_ROOM');
        if (room.hostId !== ws.fc.id) return fail(ws, 'Only the host can start the operation.', 'HOST_ONLY');
        if (![...room.players.values()].every(player => player.ready)) return fail(ws, 'Every player must be ready.', 'PLAYERS_NOT_READY');
        room.phase = 'naval_ops';
        room.missionId = 'cooperative_naval_assault';
        broadcastNotice(room, 'Co-op naval assault started. All fort attacks now share authoritative state.');
        broadcastRoom(room);
        break;
      }
      case 'navalAction': {
        const room = rooms.get(ws.fc.roomCode);
        if (!room || !room.players.has(ws.fc.id)) return fail(ws, 'Join a room first.', 'NOT_IN_ROOM');
        navalAction(ws, room, msg);
        break;
      }
      case 'leaveRoom': removeFromRoom(ws); break;
      case 'ping': send(ws, { type: 'pong', at: Date.now() }); break;
      default: fail(ws, 'Unknown message type.', 'UNKNOWN_MESSAGE');
    }
  });
  ws.on('close', () => removeFromRoom(ws));
  ws.on('error', err => console.error('WebSocket client error:', err.message));
});

const heartbeat = setInterval(() => {
  for (const ws of wss.clients) {
    if (ws.readyState !== WebSocket.OPEN) continue;
    if (ws.fc && Date.now() - ws.fc.connectedAt > 1000 * 60 * 60 * 6) {
      ws.close(1000, 'Session lifetime reached');
    }
  }
}, 30_000);

wss.on('close', () => clearInterval(heartbeat));
console.log(`Freedom Call Remastered co-op server listening on ws://0.0.0.0:${PORT}`);
console.log('LAN prototype only. Use wss/TLS, authentication, persistence and monitoring before public deployment.');
