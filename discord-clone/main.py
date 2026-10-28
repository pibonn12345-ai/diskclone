import sqlite3
import os
from datetime import datetime
from typing import Dict, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Discord Clone API")

DB_FILE = "discord.db"


# ==================== БАЗА ДАННЫХ (SQLite) ====================
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            icon TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL, -- 'text' или 'voice'
            category TEXT NOT NULL,
            FOREIGN KEY (server_id) REFERENCES servers (id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            avatar_color TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (channel_id) REFERENCES channels (id)
        )
    """)

    # Если база пустая — создаем стартовые серверы и каналы
    c.execute("SELECT COUNT(*) FROM servers")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO servers (name, icon) VALUES (?, ?)", ("Dev & Gaming Hub", "🎮"))
        s1_id = c.lastrowid
        c.execute("INSERT INTO servers (name, icon) VALUES (?, ?)", ("Python & AI Сквад", "🐍"))
        s2_id = c.lastrowid

        default_channels = [
            (s1_id, "основной-чат", "text", "ТЕКСТОВЫЕ КАНАЛЫ"),
            (s1_id, "мемы-и-скрины", "text", "ТЕКСТОВЫЕ КАНАЛЫ"),
            (s1_id, "поиск-тимейтов", "text", "ТЕКСТОВЫЕ КАНАЛЫ"),
            (s1_id, "Лобби #1", "voice", "ГОЛОСОВЫЕ КАНАЛЫ"),
            (s1_id, "Катка / Стрим", "voice", "ГОЛОСОВЫЕ КАНАЛЫ"),
            (s2_id, "общий-код", "text", "ТЕКСТОВЫЕ КАНАЛЫ"),
            (s2_id, "нейросети-и-промпты", "text", "ТЕКСТОВЫЕ КАНАЛЫ"),
            (s2_id, "Коворкинг (Voice)", "voice", "ГОЛОСОВЫЕ КАНАЛЫ"),
        ]
        c.executemany(
            "INSERT INTO channels (server_id, name, type, category) VALUES (?, ?, ?, ?)",
            default_channels
        )

        now = datetime.now().strftime("%H:%M")
        c.execute(
            "INSERT INTO messages (channel_id, username, avatar_color, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (1, "AiAssis Bot", "#5865F2", "Добро пожаловать на сервер! Переключай каналы слева, пиши в чат или заходи в Голосовой канал 🎙️", now)
        )

    conn.commit()
    conn.close()


init_db()


# ==================== МОДЕЛИ ДАННЫХ ====================
class CreateServerReq(BaseModel):
    name: str
    icon: str = "🚀"


class CreateChannelReq(BaseModel):
    server_id: int
    name: str
    type: str = "text"
    category: str = "ТЕКСТОВЫЕ КАНАЛЫ"


# ==================== REST API ====================
@app.get("/api/init")
def get_initial_data():
    conn = get_db()
    servers = [dict(row) for row in conn.execute("SELECT * FROM servers").fetchall()]
    channels = [dict(row) for row in conn.execute("SELECT * FROM channels").fetchall()]
    conn.close()
    return {"servers": servers, "channels": channels, "voice_states": manager.voice_rooms}


@app.get("/api/messages/{channel_id}")
def get_messages(channel_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE channel_id = ? ORDER BY id ASC LIMIT 100",
        (channel_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/servers")
async def create_server(req: CreateServerReq):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO servers (name, icon) VALUES (?, ?)", (req.name, req.icon))
    server_id = c.lastrowid

    c.execute(
        "INSERT INTO channels (server_id, name, type, category) VALUES (?, ?, ?, ?)",
        (server_id, "общий", "text", "ТЕКСТОВЫЕ КАНАЛЫ")
    )
    c.execute(
        "INSERT INTO channels (server_id, name, type, category) VALUES (?, ?, ?, ?)",
        (server_id, "Голосовой #1", "voice", "ГОЛОСОВЫЕ КАНАЛЫ")
    )
    conn.commit()

    server = dict(conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone())
    channels = [dict(r) for r in conn.execute("SELECT * FROM channels WHERE server_id = ?", (server_id,)).fetchall()]
    conn.close()

    await manager.broadcast({"type": "server_created", "server": server, "channels": channels})
    return server


@app.post("/api/channels")
async def create_channel(req: CreateChannelReq):
    conn = get_db()
    c = conn.cursor()
    category = "ГОЛОСОВЫЕ КАНАЛЫ" if req.type == "voice" else "ТЕКСТОВЫЕ КАНАЛЫ"
    clean_name = req.name.lower().replace(" ", "-") if req.type == "text" else req.name
    c.execute(
        "INSERT INTO channels (server_id, name, type, category) VALUES (?, ?, ?, ?)",
        (req.server_id, clean_name, req.type, category)
    )
    ch_id = c.lastrowid
    conn.commit()
    channel = dict(conn.execute("SELECT * FROM channels WHERE id = ?", (ch_id,)).fetchone())
    conn.close()

    await manager.broadcast({"type": "channel_created", "channel": channel})
    return channel


# ==================== МЕНЕДЖЕР WEBSOCKET И ГОЛОСОВЫХ КОМНАТ ====================
class ConnectionManager:
    def __init__(self):
        # client_id -> {"ws": WebSocket, "username": str, "color": str}
        self.clients: Dict[str, dict] = {}
        # channel_id (int) -> List[{"client_id": str, "username": str, "color": str, "muted": bool}]
        self.voice_rooms: Dict[int, List[dict]] = {}

    async def connect(self, client_id: str, ws: WebSocket, username: str, color: str):
        await ws.accept()
        self.clients[client_id] = {"ws": ws, "username": username, "color": color}
        await self.broadcast_online_users()
        await self.broadcast({"type": "voice_state_update", "voice_rooms": self.voice_rooms})

    async def disconnect(self, client_id: str):
        if client_id in self.clients:
            del self.clients[client_id]
        await self.leave_all_voice(client_id)
        await self.broadcast_online_users()

    async def broadcast(self, data: dict):
        dead = []
        for cid, info in self.clients.items():
            try:
                await info["ws"].send_json(data)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self.clients.pop(cid, None)

    async def send_to(self, target_id: str, data: dict):
        if target_id in self.clients:
            try:
                await self.clients[target_id]["ws"].send_json(data)
            except Exception:
                pass

    async def broadcast_online_users(self):
        users = [
            {"client_id": cid, "username": info["username"], "color": info["color"]}
            for cid, info in self.clients.items()
        ]
        await self.broadcast({"type": "online_users", "users": users})

    async def join_voice(self, client_id: str, channel_id: int):
        await self.leave_all_voice(client_id, notify=False)
        if client_id not in self.clients:
            return
        user = self.clients[client_id]
        if channel_id not in self.voice_rooms:
            self.voice_rooms[channel_id] = []

        existing_peers = [u["client_id"] for u in self.voice_rooms[channel_id]]

        self.voice_rooms[channel_id].append({
            "client_id": client_id,
            "username": user["username"],
            "color": user["color"],
            "muted": False
        })

        # Отправляем новому участнику список тех, кто уже в комнате, чтобы он инициировал WebRTC
        await self.send_to(client_id, {
            "type": "voice_peers",
            "channel_id": channel_id,
            "peers": existing_peers
        })
        await self.broadcast({"type": "voice_state_update", "voice_rooms": self.voice_rooms})

    async def leave_all_voice(self, client_id: str, notify: bool = True):
        changed = False
        for ch_id in list(self.voice_rooms.keys()):
            before = len(self.voice_rooms[ch_id])
            self.voice_rooms[ch_id] = [u for u in self.voice_rooms[ch_id] if u["client_id"] != client_id]
            if len(self.voice_rooms[ch_id]) != before:
                changed = True
                # Уведомляем оставшихся участников закрыть WebRTC соединение
                for u in self.voice_rooms[ch_id]:
                    await self.send_to(u["client_id"], {"type": "peer_left", "peer_id": client_id})
            if not self.voice_rooms[ch_id]:
                del self.voice_rooms[ch_id]
        if changed and notify:
            await self.broadcast({"type": "voice_state_update", "voice_rooms": self.voice_rooms})


manager = ConnectionManager()


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(ws: WebSocket, client_id: str, username: str = "User", color: str = "#5865F2"):
    await manager.connect(client_id, ws, username, color)
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            if msg_type == "chat_message":
                channel_id = int(data["channel_id"])
                content = data["content"].strip()
                if not content:
                    continue
                user = manager.clients.get(client_id, {"username": username, "color": color})
                now = datetime.now().strftime("%H:%M")

                conn = get_db()
                c = conn.cursor()
                c.execute(
                    "INSERT INTO messages (channel_id, username, avatar_color, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                    (channel_id, user["username"], user["color"], content, now)
                )
                msg_id = c.lastrowid
                conn.commit()
                conn.close()

                await manager.broadcast({
                    "type": "new_message",
                    "message": {
                        "id": msg_id,
                        "channel_id": channel_id,
                        "username": user["username"],
                        "avatar_color": user["color"],
                        "content": content,
                        "timestamp": now
                    }
                })

            elif msg_type == "typing":
                user = manager.clients.get(client_id, {"username": username})
                await manager.broadcast({
                    "type": "typing",
                    "channel_id": data["channel_id"],
                    "username": user["username"],
                    "client_id": client_id
                })

            elif msg_type == "update_profile":
                if client_id in manager.clients:
                    manager.clients[client_id]["username"] = data["username"]
                    manager.clients[client_id]["color"] = data["color"]
                    await manager.broadcast_online_users()

            elif msg_type == "join_voice":
                await manager.join_voice(client_id, int(data["channel_id"]))

            elif msg_type == "leave_voice":
                await manager.leave_all_voice(client_id)

            elif msg_type == "toggle_mute":
                muted = bool(data.get("muted", False))
                for ch_id, users in manager.voice_rooms.items():
                    for u in users:
                        if u["client_id"] == client_id:
                            u["muted"] = muted
                await manager.broadcast({"type": "voice_state_update", "voice_rooms": manager.voice_rooms})

            # Сигналинг WebRTC (пересылка SDP Offer/Answer и ICE кандидатов для голоса)
            elif msg_type in ("webrtc_offer", "webrtc_answer", "webrtc_ice"):
                target_id = data.get("target_id")
                if target_id:
                    data["sender_id"] = client_id
                    await manager.send_to(target_id, data)

    except WebSocketDisconnect:
        await manager.disconnect(client_id)
    except Exception:
        await manager.disconnect(client_id)


# Раздача статики (фронтенда)
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_root():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)