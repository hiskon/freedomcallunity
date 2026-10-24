# Freedom Call: Unity Remastered Co-op Server

This companion service enables real WebSocket rooms for the Remastered HTML client. It implements room creation/joining, ready checks, host-authorized mission start, and server-authoritative shared state for naval attacks and fort capture.

## Recommended: Python server

Requirements: Python 3.10+.

```bash
python -m pip install -r requirements.txt
python server.py
```

The service listens on `ws://0.0.0.0:8080` by default. Set `PORT=18765` (or another free port) before launching if you want a different port.

1. Open `freedom_call_unity_remastered_definitive.html` in two browser windows.
2. In both, open **Remastered Hub → Online Co-op** and keep the URL `ws://localhost:8080`.
3. In the first window, choose **Create room**. Copy the room code.
4. In the second window, enter the room code and choose **Join room**.
5. Both players select **Ready**. The host selects **Start co-op operation**.
6. Open **Ships & Forts** in both clients. Broadside and sabotage actions reduce fort defence; boarding captures a weakened fort. Shared ship/fort state is broadcast to the room.

For a second computer on the same LAN, use `ws://<server-LAN-IP>:8080` in the browser client and allow TCP port 8080 through the server computer's firewall. `localhost` always refers to the computer running the browser.

## Node.js alternative

Requires Node.js 18+ and npm:

```bash
npm install
npm start
```

This implementation passed a JavaScript syntax check, but npm install/runtime was not verified in the build environment. The Python version above was used for two-client browser testing.

## Protocol summary

Client messages: `createRoom`, `joinRoom`, `setReady`, `startMission`, `navalAction`, `leaveRoom`, `ping`.

Server messages: `welcome`, `roomState`, `notice`, `error`, `pong`.

The server owns room membership, ready state, phase transitions, fort defence/capture, hull/crew state and shared room currency. Clients cannot complete a fort simply by submitting a reward payload; they must issue valid actions through the server.

## Security and production limitations

This is a development/LAN prototype. Rooms and mission state live only in server memory and reset on restart. There is no account authentication, persistent progression database, public matchmaking, invite service, friends list, moderation or built-in TLS. Before exposing it to the public internet, put it behind HTTPS/WSS, add authentication and origin validation, persistent storage, stronger rate limiting and monitoring. Never put private keys or secrets in the HTML.

## HTML runtime limitations

The main game remains a browser-based HTML/JavaScript project. The expanded regional UI is not Unity additive 3D scene streaming, and CSS/Canvas effects are not URP/HDRP rendering. The existing story and Dead Kings content remain in the original HTML layer.
