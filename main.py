import sqlite3
import os
import json
from datetime import datetime
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="CordClone 2.0 API")
DB_FILE = "discord.db"


# ==================== БАЗА ДАННЫХ (SQLite) ====================
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    # Таблица пользователей
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            avatar_color TEXT NOT NULL,
            status TEXT DEFAULT 'online',
            custom_status TEXT DEFAULT ''
        )
    """)

    # Таблица друзей и заявок
    c.execute("""
        CREATE TABLE IF NOT EXISTS friendships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            friend_id TEXT NOT NULL,
            status TEXT NOT NULL, -- 'pending' или 'accepted'
            UNIQUE(user_id, friend_id)
        )
    """)

    # Серверы
    c.execute("""
        CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            icon TEXT NOT NULL
        )
    """)

    # Каналы
    c.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL, -- 'text' или 'voice'
            category TEXT NOT NULL
        )
    """)

    # Сообщения (поддерживают и каналы серверов, и ЛС через room_key)
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_key TEXT NOT NULL, -- 'ch_1' или 'dm_u1_u2'
            sender_id TEXT NOT NULL,
            username TEXT NOT NULL,
            avatar_color TEXT NOT NULL,
            content TEXT NOT NULL,
            attachment TEXT DEFAULT NULL,
            reply_to_id INTEGER DEFAULT NULL,
            reply_preview TEXT DEFAULT NULL,
            reactions TEXT DEFAULT '{}',
            edited INTEGER DEFAULT 0,
            timestamp TEXT NOT NULL
        )
    """)

    # Стартовые данные, если серверов еще нет
    c.execute("SELECT COUNT(*) FROM servers")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO servers (name, icon) VALUES (?, ?)", ("Dev & Gaming Hub", "🎮"))
        s1_id = c.lastrowid
        c.execute("INSERT INTO servers (name, icon) VALUES (?, ?)", ("Python & AI Сквад", "🐍"))
        s2_id = c.lastrowid

        default_channels = [
            (s1_id, "основной-чат", "text", "ТЕКСТОВЫЕ КАНАЛЫ"),
            (s1_id, "мемы-и-скрины", "text", "ТЕКСТОВЫЕ КАНАЛЫ"),
            (s1_id, "поиск-тиммейтов", "text", "ТЕКСТОВЫЕ КАНАЛЫ"),
            (s1_id, "Лобби #1", "voice", "ГОЛОСОВЫЕ КАНАЛЫ"),
            (s1_id, "Стрим-рум", "voice", "ГОЛОСОВЫЕ КАНАЛЫ"),
            (s2_id, "общий-код", "text", "ТЕКСТОВЫЕ КАНАЛЫ"),
            (s2_id, "нейросети-и-промпты", "text", "ТЕКСТОВЫЕ КАНАЛЫ"),
            (s2_id, "Коворкинг (Voice)", "voice", "ГОЛОСОВЫЕ КАНАЛЫ"),
        ]
        c.executemany(
            "INSERT INTO channels (server_id, name, type, category) VALUES (?, ?, ?, ?)",
            default_channels
        )

        now = datetime.now().strftime("%H:%M")
        c.execute("""
            INSERT INTO messages_v2 (room_key, sender_id, username, avatar_color, content, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            "ch_1", "system_bot", "AiAssis Bot", "#5865F2",
            "Добро пожаловать в **CordClone 2.0**! 🎉\n"
            "• Нажми на иконку **Discord** слева сверху, чтобы добавлять друзей и писать в **ЛС**.\n"
            "• Кликни на любого участника, чтобы открыть его профиль, добавить в друзья или позвонить.\n"
            "• В настройках **⚙️** доступен режим рации (**Push-to-Talk**), выбор микрофона и темы!\n"
            "• В голосовых каналах и звонках ЛС работают **Демонстрация экрана 🖥️** и **Веб-камера 🎥**.",
            now
        ))

    conn.commit()
    conn.close()


init_db()


# ==================== МОДЕЛИ ЗАПРОСОВ ====================
class CreateServerReq(BaseModel):
    name: str
    icon: str = "🚀"


class CreateChannelReq(BaseModel):
    server_id: int
    name: str
    type: str = "text"


class FriendRequestModel(BaseModel):
    from_user_id: str
    target_query: str  # Никнейм или ID


class FriendActionModel(BaseModel):
    user_id: str
    friend_id: str
    action: str  # 'accept' или 'reject'


def make_dm_key(u1: str, u2: str) -> str:
    pair = sorted([u1, u2])
    return f"dm_{pair[0]}_{pair[1]}"


# ==================== REST API ====================
@app.get("/api/init")
def get_initial_data(user_id: str = "", username: str = "User", color: str = "#5865F2"):
    conn = get_db()
    c = conn.cursor()

    if user_id:
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        existing = c.fetchone()
        if not existing:
            c.execute(
                "INSERT INTO users (user_id, username, avatar_color, status, custom_status) VALUES (?, ?, ?, 'online', '')",
                (user_id, username, color)
            )
            conn.commit()

    servers = [dict(r) for r in c.execute("SELECT * FROM servers").fetchall()]
    channels = [dict(r) for r in c.execute("SELECT * FROM channels").fetchall()]
    users = [dict(r) for r in c.execute("SELECT * FROM users").fetchall()]
    conn.close()

    return {
        "servers": servers,
        "channels": channels,
        "users": users,
        "voice_states": manager.voice_rooms,
        "online_ids": list(manager.clients.keys())
    }


@app.get("/api/messages/{room_key}")
def get_messages(room_key: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM messages_v2 WHERE room_key = ? ORDER BY id ASC LIMIT 150",
        (room_key,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["reactions"] = json.loads(d["reactions"] or "{}")
        except Exception:
            d["reactions"] = {}
        result.append(d)
    return result


@app.get("/api/friends/{user_id}")
def get_friends(user_id: str):
    conn = get_db()
    c = conn.cursor()

    # Все записи дружбы, где участвует пользователь
    rows = c.execute("""
        SELECT f.*, 
               u1.username AS user_name, u1.avatar_color AS user_color, u1.status AS user_status, u1.custom_status AS user_custom,
               u2.username AS friend_name, u2.avatar_color AS friend_color, u2.status AS friend_status, u2.custom_status AS friend_custom
        FROM friendships f
        LEFT JOIN users u1 ON f.user_id = u1.user_id
        LEFT JOIN users u2 ON f.friend_id = u2.user_id
        WHERE f.user_id = ? OR f.friend_id = ?
    """, (user_id, user_id)).fetchall()
    conn.close()

    friends = []
    incoming = []
    outgoing = []

    for r in rows:
        if r["status"] == "accepted":
            is_sender = (r["user_id"] == user_id)
            other_id = r["friend_id"] if is_sender else r["user_id"]
            friends.append({
                "user_id": other_id,
                "username": r["friend_name"] if is_sender else r["user_name"],
                "avatar_color": r["friend_color"] if is_sender else r["user_color"],
                "status": r["friend_status"] if is_sender else r["user_status"],
                "custom_status": r["friend_custom"] if is_sender else r["user_custom"],
                "online": other_id in manager.clients
            })
        elif r["status"] == "pending":
            if r["friend_id"] == user_id:
                incoming.append({
                    "user_id": r["user_id"],
                    "username": r["user_name"],
                    "avatar_color": r["user_color"]
                })
            else:
                outgoing.append({
                    "user_id": r["friend_id"],
                    "username": r["friend_name"],
                    "avatar_color": r["friend_color"]
                })

    return {"friends": friends, "incoming": incoming, "outgoing": outgoing}


@app.post("/api/friends/request")
async def send_friend_request(req: FriendRequestModel):
    conn = get_db()
    c = conn.cursor()

    target_q = req.target_query.strip()
    target = c.execute(
        "SELECT * FROM users WHERE user_id = ? OR LOWER(username) = LOWER(?)",
        (target_q, target_q)
    ).fetchone()

    if not target:
        conn.close()
        return {"ok": False, "error": "Пользователь с таким никнеймом или ID не найден"}

    target_id = target["user_id"]
    if target_id == req.from_user_id:
        conn.close()
        return {"ok": False, "error": "Нельзя добавить самого себя"}

    existing = c.execute("""
        SELECT * FROM friendships
        WHERE (user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?)
    """, (req.from_user_id, target_id, target_id, req.from_user_id)).fetchone()

    if existing:
        conn.close()
        if existing["status"] == "accepted":
            return {"ok": False, "error": "Вы уже друзья!"}
        return {"ok": False, "error": "Заявка уже отправлена"}

    c.execute(
        "INSERT INTO friendships (user_id, friend_id, status) VALUES (?, ?, 'pending')",
        (req.from_user_id, target_id)
    )
    conn.commit()
    conn.close()

    await manager.send_to(target_id, {"type": "friend_update"})
    await manager.send_to(req.from_user_id, {"type": "friend_update"})
    return {"ok": True, "message": f"Заявка отправлена пользователю {target['username']}!"}


@app.post("/api/friends/respond")
async def respond_friend_request(req: FriendActionModel):
    conn = get_db()
    c = conn.cursor()
    if req.action == "accept":
        c.execute("""
            UPDATE friendships SET status = 'accepted'
            WHERE (user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?)
        """, (req.friend_id, req.user_id, req.user_id, req.friend_id))
    else:
        c.execute("""
            DELETE FROM friendships
            WHERE (user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?)
        """, (req.friend_id, req.user_id, req.user_id, req.friend_id))
    conn.commit()
    conn.close()

    await manager.send_to(req.user_id, {"type": "friend_update"})
    await manager.send_to(req.friend_id, {"type": "friend_update"})
    return {"ok": True}


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


# ==================== WEBSOCKET МЕНЕДЖЕР ====================
class ConnectionManager:
    def __init__(self):
        # user_id -> {"ws": WebSocket, "username": str, "color": str, "status": str, "custom_status": str}
        self.clients: Dict[str, dict] = {}
        # room_id (str, напр. 'ch_4' или 'dm_u1_u2') -> List[dict]
        self.voice_rooms: Dict[str, List[dict]] = {}

    async def connect(self, user_id: str, ws: WebSocket, username: str, color: str, status: str, custom_status: str):
        await ws.accept()
        self.clients[user_id] = {
            "ws": ws,
            "username": username,
            "color": color,
            "status": status,
            "custom_status": custom_status
        }
        conn = get_db()
        conn.execute("""
            INSERT INTO users (user_id, username, avatar_color, status, custom_status)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                avatar_color=excluded.avatar_color,
                status=excluded.status,
                custom_status=excluded.custom_status
        """, (user_id, username, color, status, custom_status))
        conn.commit()
        conn.close()

        await self.broadcast_online_users()
        await self.broadcast({"type": "voice_state_update", "voice_rooms": self.voice_rooms})

    async def disconnect(self, user_id: str):
        self.clients.pop(user_id, None)
        await self.leave_all_voice(user_id)
        await self.broadcast_online_users()

    async def broadcast(self, data: dict):
        dead = []
        for uid, info in list(self.clients.items()):
            try:
                await info["ws"].send_json(data)
            except Exception:
                dead.append(uid)
        for uid in dead:
            self.clients.pop(uid, None)

    async def send_to(self, target_id: str, data: dict):
        if target_id in self.clients:
            try:
                await self.clients[target_id]["ws"].send_json(data)
            except Exception:
                pass

    async def broadcast_online_users(self):
        conn = get_db()
        all_db_users = [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()]
        conn.close()

        users = []
        for u in all_db_users:
            uid = u["user_id"]
            is_online = uid in self.clients and u["status"] != "invisible"
            users.append({
                "user_id": uid,
                "username": u["username"],
                "color": u["avatar_color"],
                "status": u["status"] if uid in self.clients else "offline",
                "custom_status": u["custom_status"],
                "online": is_online
            })
        await self.broadcast({"type": "online_users", "users": users})

    async def join_voice(self, user_id: str, room_id: str):
        await self.leave_all_voice(user_id, notify=False)
        if user_id not in self.clients:
            return
        u = self.clients[user_id]
        if room_id not in self.voice_rooms:
            self.voice_rooms[room_id] = []

        existing_peers = [p["user_id"] for p in self.voice_rooms[room_id] if p["user_id"] != user_id]

        self.voice_rooms[room_id].append({
            "user_id": user_id,
            "username": u["username"],
            "color": u["color"],
            "muted": False,
            "deafened": False,
            "video": False,
            "screen": False
        })

        await self.send_to(user_id, {
            "type": "voice_peers",
            "room_id": room_id,
            "peers": existing_peers
        })
        await self.broadcast({"type": "voice_state_update", "voice_rooms": self.voice_rooms})

    async def leave_all_voice(self, user_id: str, notify: bool = True):
        changed = False
        for r_id in list(self.voice_rooms.keys()):
            before = len(self.voice_rooms[r_id])
            self.voice_rooms[r_id] = [u for u in self.voice_rooms[r_id] if u["user_id"] != user_id]
            if len(self.voice_rooms[r_id]) != before:
                changed = True
                for u in self.voice_rooms[r_id]:
                    await self.send_to(u["user_id"], {"type": "peer_left", "peer_id": user_id})
            if not self.voice_rooms[r_id]:
                del self.voice_rooms[r_id]
        if changed and notify:
            await self.broadcast({"type": "voice_state_update", "voice_rooms": self.voice_rooms})


manager = ConnectionManager()


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(
    ws: WebSocket,
    user_id: str,
    username: str = "User",
    color: str = "#5865F2",
    status: str = "online",
    custom_status: str = ""
):
    await manager.connect(user_id, ws, username, color, status, custom_status)
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            # 1. ОТПРАВКА СООБЩЕНИЯ (В КАНАЛ ИЛИ В ЛС)
            if msg_type == "chat_message":
                room_key = str(data["room_key"])
                content = data.get("content", "").strip()
                attachment = data.get("attachment")
                reply_to_id = data.get("reply_to_id")
                reply_preview = data.get("reply_preview")

                if not content and not attachment:
                    continue

                u = manager.clients.get(user_id, {"username": username, "color": color})
                now = datetime.now().strftime("%H:%M")

                conn = get_db()
                c = conn.cursor()
                c.execute("""
                    INSERT INTO messages_v2
                    (room_key, sender_id, username, avatar_color, content, attachment, reply_to_id, reply_preview, reactions, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                """, (room_key, user_id, u["username"], u["color"], content, attachment, reply_to_id, reply_preview, now))
                msg_id = c.lastrowid
                conn.commit()
                conn.close()

                payload = {
                    "type": "new_message",
                    "message": {
                        "id": msg_id,
                        "room_key": room_key,
                        "sender_id": user_id,
                        "username": u["username"],
                        "avatar_color": u["color"],
                        "content": content,
                        "attachment": attachment,
                        "reply_to_id": reply_to_id,
                        "reply_preview": reply_preview,
                        "reactions": {},
                        "edited": 0,
                        "timestamp": now
                    }
                }
                await manager.broadcast(payload)

            # 2. РЕДАКТИРОВАНИЕ СООБЩЕНИЯ
            elif msg_type == "edit_message":
                msg_id = int(data["message_id"])
                new_content = data.get("content", "").strip()
                if not new_content:
                    continue
                conn = get_db()
                conn.execute(
                    "UPDATE messages_v2 SET content = ?, edited = 1 WHERE id = ? AND sender_id = ?",
                    (new_content, msg_id, user_id)
                )
                conn.commit()
                conn.close()
                await manager.broadcast({
                    "type": "message_edited",
                    "message_id": msg_id,
                    "content": new_content
                })

            # 3. УДАЛЕНИЕ СООБЩЕНИЯ
            elif msg_type == "delete_message":
                msg_id = int(data["message_id"])
                conn = get_db()
                conn.execute("DELETE FROM messages_v2 WHERE id = ? AND sender_id = ?", (msg_id, user_id))
                conn.commit()
                conn.close()
                await manager.broadcast({"type": "message_deleted", "message_id": msg_id})

            # 4. РЕАКЦИИ ЭМОДЗИ НА СООБЩЕНИЕ
            elif msg_type == "toggle_reaction":
                msg_id = int(data["message_id"])
                emoji = data["emoji"]
                conn = get_db()
                row = conn.execute("SELECT reactions FROM messages_v2 WHERE id = ?", (msg_id,)).fetchone()
                if row:
                    try:
                        reactions = json.loads(row["reactions"] or "{}")
                    except Exception:
                        reactions = {}
                    users_list = reactions.get(emoji, [])
                    if user_id in users_list:
                        users_list.remove(user_id)
                    else:
                        users_list.append(user_id)
                    if users_list:
                        reactions[emoji] = users_list
                    else:
                        reactions.pop(emoji, None)
                    conn.execute("UPDATE messages_v2 SET reactions = ? WHERE id = ?", (json.dumps(reactions), msg_id))
                    conn.commit()
                    await manager.broadcast({
                        "type": "message_reactions",
                        "message_id": msg_id,
                        "reactions": reactions
                    })
                conn.close()

            # 5. ИНДИКАТОР НАБОРА ТЕКСТА
            elif msg_type == "typing":
                u = manager.clients.get(user_id, {"username": username})
                await manager.broadcast({
                    "type": "typing",
                    "room_key": data["room_key"],
                    "username": u["username"],
                    "user_id": user_id
                })

            # 6. ОБНОВЛЕНИЕ ПРОФИЛЯ И СТАТУСА
            elif msg_type == "update_profile":
                new_name = data.get("username", username)
                new_color = data.get("color", color)
                new_status = data.get("status", "online")
                new_custom = data.get("custom_status", "")

                if user_id in manager.clients:
                    manager.clients[user_id].update({
                        "username": new_name,
                        "color": new_color,
                        "status": new_status,
                        "custom_status": new_custom
                    })
                conn = get_db()
                conn.execute("""
                    UPDATE users SET username = ?, avatar_color = ?, status = ?, custom_status = ?
                    WHERE user_id = ?
                """, (new_name, new_color, new_status, new_custom, user_id))
                conn.commit()
                conn.close()
                await manager.broadcast_online_users()
                await manager.broadcast({"type": "friend_update"})

            # 7. ГОЛОСОВЫЕ КОМНАТЫ И WEBRTC
            elif msg_type == "join_voice":
                await manager.join_voice(user_id, str(data["room_id"]))

            elif msg_type == "leave_voice":
                await manager.leave_all_voice(user_id)

            elif msg_type == "voice_media_state":
                for r_id, users_list in manager.voice_rooms.items():
                    for u in users_list:
                        if u["user_id"] == user_id:
                            u["muted"] = bool(data.get("muted", u["muted"]))
                            u["deafened"] = bool(data.get("deafened", u["deafened"]))
                            u["video"] = bool(data.get("video", u["video"]))
                            u["screen"] = bool(data.get("screen", u["screen"]))
                await manager.broadcast({"type": "voice_state_update", "voice_rooms": manager.voice_rooms})

            # 8. ЗВОНКИ ДРУГУ В ЛС (ВХОДЯЩИЙ ВЫЗОВ)
            elif msg_type == "call_user":
                target_id = data.get("target_id")
                room_id = data.get("room_id")
                with_video = bool(data.get("with_video", False))
                caller = manager.clients.get(user_id, {"username": username, "color": color})
                if target_id:
                    await manager.send_to(target_id, {
                        "type": "incoming_call",
                        "caller_id": user_id,
                        "caller_name": caller["username"],
                        "caller_color": caller["color"],
                        "room_id": room_id,
                        "with_video": with_video
                    })

            elif msg_type == "call_response":
                caller_id = data.get("caller_id")
                accepted = bool(data.get("accepted", False))
                room_id = data.get("room_id")
                if caller_id:
                    await manager.send_to(caller_id, {
                        "type": "call_responded",
                        "responder_id": user_id,
                        "accepted": accepted,
                        "room_id": room_id
                    })

            # 9. WEBRTC СИГНАЛИНГ
            elif msg_type in ("webrtc_offer", "webrtc_answer", "webrtc_ice"):
                target_id = data.get("target_id")
                if target_id:
                    data["sender_id"] = user_id
                    await manager.send_to(target_id, data)

    except WebSocketDisconnect:
        await manager.disconnect(user_id)
    except Exception:
        await manager.disconnect(user_id)


os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_root():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)