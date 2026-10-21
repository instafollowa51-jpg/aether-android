import os
import io
import json
import sqlite3
import asyncio
from typing import Optional
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, Response, FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeFilename
from mutagen.flac import FLAC

# Use /tmp/aether_data because Hugging Face Spaces (/app) is read-only for UID 1000
DATA_DIR = "/tmp/aether_data"
COVERS_DIR = os.path.join(DATA_DIR, "covers")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "catalogue.db")
SESSION_PATH = os.path.join(DATA_DIR, "aether_lossless_session")

os.makedirs(COVERS_DIR, exist_ok=True)

app = FastAPI(title="Aether Lossless Telegram Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client: Optional[TelegramClient] = None

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            msg_id INTEGER PRIMARY KEY,
            title TEXT,
            artist TEXT,
            album TEXT,
            genre TEXT,
            year TEXT,
            track_num INTEGER,
            duration REAL,
            bit_depth INTEGER,
            sample_rate INTEGER,
            channels INTEGER,
            file_size INTEGER,
            mime_type TEXT,
            has_cover INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

init_db()

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return {
        "api_id": int(os.getenv("TG_API_ID", "0")),
        "api_hash": os.getenv("TG_API_HASH", ""),
        "channel_id": int(os.getenv("TG_CHANNEL_ID", "0"))
    }

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

async def ensure_client():
    global client
    cfg = load_config()
    if not cfg["api_id"] or not cfg["api_hash"]:
        return None
    if client is None:
        client = TelegramClient(SESSION_PATH, int(cfg["api_id"]), cfg["api_hash"])
        await client.connect()
    return client

class SetupRequest(BaseModel):
    api_id: int
    api_hash: str
    channel_id: int
    phone: str

class VerifyRequest(BaseModel):
    phone: str
    code: str
    password: Optional[str] = None

@app.get("/api/status")
async def get_status():
    cfg = load_config()
    if not cfg["api_id"] or not cfg["api_hash"] or not cfg["channel_id"]:
        return {"configured": False, "authorized": False}
    cl = await ensure_client()
    if not cl:
        return {"configured": False, "authorized": False}
    authorized = await cl.is_user_authorized()
    return {"configured": True, "authorized": authorized, "channel_id": cfg["channel_id"]}

@app.post("/api/auth/send_code")
async def send_auth_code(req: SetupRequest):
    global client
    save_config({"api_id": req.api_id, "api_hash": req.api_hash, "channel_id": req.channel_id})
    if client:
        await client.disconnect()
    client = TelegramClient(SESSION_PATH, req.api_id, req.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(req.phone)
        return {"status": "code_sent"}
    return {"status": "already_authorized"}

@app.post("/api/auth/verify")
async def verify_auth_code(req: VerifyRequest):
    cl = await ensure_client()
    if not cl:
        raise HTTPException(status_code=400, detail="Client not initialized")
    try:
        await cl.sign_in(req.phone, req.code)
    except Exception as e:
        if "SessionPasswordNeededError" in str(type(e)) and req.password:
            await cl.sign_in(password=req.password)
        else:
            raise HTTPException(status_code=400, detail=str(e))
    return {"status": "authorized"}

async def extract_flac_header_metadata(cl: TelegramClient, msg):
    header_bytes = bytearray()
    async for chunk in cl.iter_download(msg.document, offset=0, request_size=65536):
        header_bytes.extend(chunk)
        if len(header_bytes) >= 524288:
            break

    meta = {
        "title": None,
        "artist": None,
        "album": "Single / Unknown Album",
        "genre": "Lossless",
        "year": "",
        "track_num": 0,
        "duration": 0.0,
        "bit_depth": 16,
        "sample_rate": 44100,
        "channels": 2,
        "has_cover": 0
    }

    for attr in msg.document.attributes:
        if isinstance(attr, DocumentAttributeAudio):
            meta["title"] = attr.title
            meta["artist"] = attr.performer
            meta["duration"] = float(attr.duration or 0)
        elif isinstance(attr, DocumentAttributeFilename):
            if not meta["title"]:
                meta["title"] = os.path.splitext(attr.file_name)[0]

    cover_file = os.path.join(COVERS_DIR, f"{msg.id}.jpg")
    try:
        bio = io.BytesIO(bytes(header_bytes))
        flac = FLAC(bio)
        if flac.info:
            meta["bit_depth"] = getattr(flac.info, "bits_per_sample", 16)
            meta["sample_rate"] = getattr(flac.info, "sample_rate", 44100)
            meta["channels"] = getattr(flac.info, "channels", 2)
            meta["duration"] = float(getattr(flac.info, "length", meta["duration"]))
        if flac.tags:
            meta["title"] = flac.tags.get("title", [meta["title"]])[0]
            meta["artist"] = flac.tags.get("artist", [meta["artist"] or "Unknown Artist"])[0]
            meta["album"] = flac.tags.get("album", [meta["album"]])[0]
            meta["genre"] = flac.tags.get("genre", [meta["genre"]])[0]
            meta["year"] = flac.tags.get("date", flac.tags.get("year", [""]))[0]
            tr = flac.tags.get("tracknumber", ["0"])[0]
            meta["track_num"] = int(str(tr).split("/")[0]) if str(tr).split("/")[0].isdigit() else 0
        if flac.pictures:
            pic = flac.pictures[0]
            with open(cover_file, "wb") as cf:
                cf.write(pic.data)
            meta["has_cover"] = 1
    except Exception:
        try:
            thumb = await cl.download_media(msg, thumb=-1, file=bytes)
            if thumb:
                with open(cover_file, "wb") as cf:
                    cf.write(thumb)
                meta["has_cover"] = 1
        except Exception:
            pass

    if not meta["title"]:
        meta["title"] = f"Track {msg.id}"
    if not meta["artist"]:
        meta["artist"] = "Unknown Artist"
    return meta

@app.post("/api/sync")
async def sync_catalogue():
    cl = await ensure_client()
    cfg = load_config()
    if not cl or not await cl.is_user_authorized():
        raise HTTPException(status_code=401, detail="Telegram not authorized")

    channel_id = int(cfg["channel_id"])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT msg_id FROM tracks")
    existing_ids = {row[0] for row in c.fetchall()}

    added = 0
    async for msg in cl.iter_messages(channel_id, limit=500):
        if msg.id in existing_ids:
            continue
        if msg.document and msg.file:
            mime = (msg.file.mime_type or "").lower()
            fname = (msg.file.name or "").lower()
            if "audio" in mime or fname.endswith((".flac", ".m4a", ".alac", ".wav")):
                meta = await extract_flac_header_metadata(cl, msg)
                c.execute("""
                    INSERT OR REPLACE INTO tracks 
                    (msg_id, title, artist, album, genre, year, track_num, duration, bit_depth, sample_rate, channels, file_size, mime_type, has_cover)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    msg.id, meta["title"], meta["artist"], meta["album"], meta["genre"],
                    meta["year"], meta["track_num"], meta["duration"], meta["bit_depth"],
                    meta["sample_rate"], meta["channels"], msg.file.size,
                    msg.file.mime_type or "audio/flac", meta["has_cover"]
                ))
                conn.commit()
                added += 1

    conn.close()
    return {"status": "synced", "new_tracks_indexed": added}

@app.get("/api/catalogue")
async def get_catalogue():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tracks ORDER BY album ASC, track_num ASC, msg_id DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

@app.get("/api/cover/{msg_id}")
async def get_cover(msg_id: int):
    cover_path = os.path.join(COVERS_DIR, f"{msg_id}.jpg")
    if os.path.exists(cover_path):
        return FileResponse(cover_path, media_type="image/jpeg")
    cl = await ensure_client()
    cfg = load_config()
    if cl and await cl.is_user_authorized():
        msg = await cl.get_messages(int(cfg["channel_id"]), ids=msg_id)
        if msg:
            thumb = await cl.download_media(msg, thumb=-1, file=bytes)
            if thumb:
                with open(cover_path, "wb") as f:
                    f.write(thumb)
                return Response(content=thumb, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Cover not found")

@app.get("/api/stream/{msg_id}")
async def stream_lossless(msg_id: int, request: Request):
    cl = await ensure_client()
    cfg = load_config()
    if not cl or not await cl.is_user_authorized():
        raise HTTPException(status_code=401, detail="Unauthorized")

    msg = await cl.get_messages(int(cfg["channel_id"]), ids=msg_id)
    if not msg or not msg.document:
        raise HTTPException(status_code=404, detail="Track not found in Telegram channel")

    file_size = msg.file.size
    range_header = request.headers.get("Range")
    offset = 0
    end = file_size - 1

    if range_header:
        parts = range_header.replace("bytes=", "").split("-")
        offset = int(parts[0]) if parts[0] else 0
        if len(parts) > 1 and parts[1]:
            end = int(parts[1])

    aligned_offset = offset - (offset % 4096)
    skip_bytes = offset - aligned_offset
    content_length = end - offset + 1

    async def chunk_generator():
        first = True
        remaining = content_length
        async for chunk in cl.iter_download(msg.document, offset=aligned_offset, request_size=65536):
            if first and skip_bytes:
                chunk = chunk[skip_bytes:]
                first = False
            if len(chunk) >= remaining:
                yield chunk[:remaining]
                break
            remaining -= len(chunk)
            yield chunk

    headers = {
        "Content-Range": f"bytes {offset}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Cache-Control": "public, max-age=86400"
    }
    return StreamingResponse(
        chunk_generator(),
        status_code=206 if range_header else 200,
        media_type=msg.file.mime_type or "audio/flac",
        headers=headers
    )

INDEX_HTML = '<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="UTF-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />\n  <title>Aether • Lossless Music Streamer</title>\n  <link rel="manifest" href="/manifest.json" />\n  <style>\n    :root {\n      --glass-bg: rgba(255, 255, 255, 0.07);\n      --glass-border: rgba(255, 255, 255, 0.16);\n      --accent: #ff2d55;\n    }\n    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif; }\n    body {\n      background: #07080c; color: #fff; height: 100vh; width: 100vw;\n      overflow: hidden; display: flex; flex-direction: column; position: relative;\n    }\n    .ambient-canvas {\n      position: fixed; inset: -60px; background-size: cover; background-position: center;\n      filter: blur(85px) brightness(0.42) saturate(2.1); transform: scale(1.25);\n      transition: background-image 0.9s cubic-bezier(0.22, 1, 0.36, 1); z-index: 0;\n    }\n    .app-layout {\n      position: relative; z-index: 1; display: grid;\n      grid-template-columns: 240px 1fr 340px; gap: 16px;\n      height: calc(100vh - 104px); padding: 16px 16px 8px 16px;\n    }\n    .glass-card {\n      background: var(--glass-bg);\n      backdrop-filter: blur(40px) saturate(190%);\n      -webkit-backdrop-filter: blur(40px) saturate(190%);\n      border: 1px solid var(--glass-border);\n      border-radius: 24px; padding: 18px; overflow-y: auto;\n      box-shadow: 0 20px 50px rgba(0,0,0,0.45);\n    }\n    /* Sidebar Navigation */\n    .brand { font-size: 20px; font-weight: 800; letter-spacing: -0.5px; margin-bottom: 22px; display: flex; align-items: center; gap: 10px; }\n    .brand span { background: linear-gradient(135deg, #fff, #a5b4fc); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }\n    .nav-btn {\n      width: 100%; text-align: left; background: transparent; border: none; color: rgba(255,255,255,0.75);\n      padding: 11px 14px; border-radius: 14px; font-size: 14px; font-weight: 600; cursor: pointer;\n      transition: 0.2s; margin-bottom: 6px; display: flex; align-items: center; justify-content: space-between;\n    }\n    .nav-btn:hover, .nav-btn.active { background: rgba(255,255,255,0.14); color: #fff; }\n    .sync-btn {\n      margin-top: auto; width: 100%; padding: 12px; border-radius: 14px; border: 1px solid rgba(255,255,255,0.25);\n      background: rgba(255,255,255,0.1); color: #fff; font-weight: 700; cursor: pointer; transition: 0.2s;\n    }\n    .sync-btn:hover { background: rgba(255,255,255,0.22); }\n    /* Center Catalogue */\n    .top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; gap: 12px; }\n    .search-input {\n      flex: 1; background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15);\n      padding: 10px 16px; border-radius: 99px; color: #fff; font-size: 14px; outline: none;\n    }\n    .grid-albums { display: grid; grid-template-columns: repeat(auto-fill, minmax(145px, 1fr)); gap: 16px; }\n    .album-card {\n      background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);\n      padding: 10px; border-radius: 18px; cursor: pointer; transition: transform 0.2s, background 0.2s;\n    }\n    .album-card:hover { transform: translateY(-4px); background: rgba(255,255,255,0.12); }\n    .album-card img { width: 100%; aspect-ratio: 1; border-radius: 12px; object-fit: cover; margin-bottom: 8px; background: #1a1c23; }\n    .song-row {\n      display: grid; grid-template-columns: 46px 1fr 180px 110px 60px; align-items: center;\n      gap: 12px; padding: 8px 12px; border-radius: 14px; cursor: pointer; transition: 0.15s;\n      border-bottom: 1px solid rgba(255,255,255,0.04);\n    }\n    .song-row:hover, .song-row.active { background: rgba(255,255,255,0.13); }\n    .song-row img { width: 40px; height: 40px; border-radius: 8px; object-fit: cover; background: #1a1c23; }\n    .hi-res-pill {\n      font-size: 10px; font-weight: 700; padding: 3px 7px; border-radius: 6px;\n      background: rgba(255, 214, 10, 0.18); color: #ffd60a; border: 1px solid rgba(255, 214, 10, 0.35);\n      text-align: center; width: fit-content;\n    }\n    /* Right Panel: Lyrics & EQ */\n    .tab-header { display: flex; gap: 8px; margin-bottom: 14px; }\n    .tab-pill {\n      flex: 1; padding: 8px; border-radius: 10px; border: none; background: rgba(255,255,255,0.07);\n      color: #fff; font-size: 12px; font-weight: 600; cursor: pointer;\n    }\n    .tab-pill.active { background: rgba(255,255,255,0.22); }\n    .lyric-line {\n      font-size: 16px; font-weight: 600; opacity: 0.32; margin: 14px 0;\n      transition: all 0.35s ease; cursor: pointer; line-height: 1.4;\n    }\n    .lyric-line.active {\n      opacity: 1; font-size: 20px; transform: scale(1.02); transform-origin: left;\n      text-shadow: 0 0 24px rgba(255,255,255,0.65);\n    }\n    /* Bottom Player Bar */\n    .player-dock {\n      position: relative; z-index: 2; height: 88px; margin: 0 16px 12px 16px;\n      display: grid; grid-template-columns: 300px 1fr 300px; align-items: center; padding: 0 20px;\n    }\n    .now-info { display: flex; align-items: center; gap: 14px; }\n    .now-info img { width: 56px; height: 56px; border-radius: 12px; object-fit: cover; box-shadow: 0 8px 20px rgba(0,0,0,0.4); }\n    .transport { display: flex; flex-direction: column; align-items: center; gap: 6px; }\n    .ctrl-btns { display: flex; align-items: center; gap: 18px; }\n    .btn-circle {\n      width: 40px; height: 40px; border-radius: 50%; border: none; background: #fff; color: #000;\n      font-size: 16px; font-weight: bold; cursor: pointer; display: flex; align-items: center; justify-content: center;\n    }\n    .btn-icon { background: transparent; border: none; color: #fff; font-size: 15px; cursor: pointer; opacity: 0.8; }\n    .btn-icon:hover { opacity: 1; }\n    .seek-bar { width: 100%; max-width: 480px; display: flex; align-items: center; gap: 10px; font-size: 11px; opacity: 0.8; }\n    .seek-bar input { flex: 1; accent-color: #fff; cursor: pointer; }\n    /* Modal for Telegram Setup */\n    .modal-backdrop {\n      position: fixed; inset: 0; background: rgba(0,0,0,0.75); backdrop-filter: blur(20px);\n      z-index: 50; display: none; align-items: center; justify-content: center;\n    }\n    .modal-box { width: 420px; max-width: 92vw; }\n    .field { margin-bottom: 12px; }\n    .field label { display: block; font-size: 12px; opacity: 0.75; margin-bottom: 4px; }\n    .field input {\n      width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.2);\n      background: rgba(255,255,255,0.08); color: #fff;\n    }\n    @media (max-width: 900px) {\n      .app-layout { grid-template-columns: 1fr; height: calc(100vh - 150px); }\n      .sidebar-card { display: none; }\n      .player-dock { grid-template-columns: 1fr; height: auto; padding: 12px; gap: 10px; }\n    }\n  </style>\n</head>\n<body>\n  <div class="ambient-canvas" id="ambientBg"></div>\n\n  <div class="app-layout">\n    <!-- Left Sidebar -->\n    <div class="glass-card sidebar-card" style="display:flex; flex-direction:column;">\n      <div class="brand">◈ <span>Aether Lossless</span></div>\n      <button class="nav-btn active" onclick="setView(\'songs\', this)"><span>All Songs</span><small id="countSongs">0</small></button>\n      <button class="nav-btn" onclick="setView(\'albums\', this)"><span>Albums</span><small id="countAlbums">0</small></button>\n      <button class="nav-btn" onclick="setView(\'artists\', this)"><span>Artists</span><small id="countArtists">0</small></button>\n      <button class="nav-btn" onclick="setView(\'hires\', this)"><span>24-Bit Hi-Res</span><small id="countHiRes">0</small></button>\n\n      <div style="margin-top:auto; display:flex; flex-direction:column; gap:8px;">\n        <button class="sync-btn" onclick="syncTelegram()" id="syncBtn">↻ Sync Telegram Vault</button>\n        <button class="nav-btn" onclick="openSetupModal()" style="font-size:12px; justify-content:center;">⚙ Telegram Settings</button>\n      </div>\n    </div>\n\n    <!-- Center Library Catalogue -->\n    <div class="glass-card" id="mainView">\n      <div class="top-bar">\n        <h2 id="viewTitle" style="font-size: 22px;">Lossless Catalogue</h2>\n        <input type="text" class="search-input" id="searchInput" placeholder="Search songs, albums, artists, or 24-bit..." oninput="renderCatalogue()" />\n      </div>\n      <div id="catalogueContent"></div>\n    </div>\n\n    <!-- Right Panel: Synced Lyrics + Audiophile DSP -->\n    <div class="glass-card" style="display:flex; flex-direction:column;">\n      <div class="tab-header">\n        <button class="tab-pill active" id="tabLyricsBtn" onclick="switchRightTab(\'lyrics\')">Synced Lyrics</button>\n        <button class="tab-pill" id="tabEqBtn" onclick="switchRightTab(\'eq\')">Audio Engine & EQ</button>\n      </div>\n\n      <!-- Lyrics View -->\n      <div id="lyricsPane" style="flex:1; overflow-y:auto;">\n        <p style="opacity:0.5; font-size:14px;">Select any lossless track from your catalogue to stream and load time-synced lyrics.</p>\n      </div>\n\n      <!-- Audiophile DSP & Bit-Perfect Toggle View -->\n      <div id="eqPane" style="display:none; flex:1; overflow-y:auto;">\n        <div style="background:rgba(255,255,255,0.06); padding:12px; border-radius:14px; margin-bottom:14px;">\n          <label style="display:flex; justify-content:space-between; align-items:center; font-size:13px; font-weight:700; cursor:pointer;">\n            <span>Bit-Perfect Direct Stream (Bypass DSP)</span>\n            <input type="checkbox" id="bitPerfectToggle" checked onchange="toggleBitPerfect()" />\n          </label>\n          <p style="font-size:11px; opacity:0.65; margin-top:6px;">\n            When enabled, audio bypasses browser DSP filters for pure untouched 16-bit/24-bit FLAC output. Turn off to engage the 5-band Equalizer & Spatial Crossfeed.\n          </p>\n        </div>\n\n        <div id="dspControls" style="opacity:0.4; pointer-events:none;">\n          <div class="field"><label>Sub-Bass (60 Hz)</label><input type="range" id="eq60" min="-12" max="12" value="0" /></div>\n          <div class="field"><label>Low-Mid (250 Hz)</label><input type="range" id="eq250" min="-12" max="12" value="0" /></div>\n          <div class="field"><label>Vocal Presence (1 kHz)</label><input type="range" id="eq1k" min="-12" max="12" value="0" /></div>\n          <div class="field"><label>Clarity (4 kHz)</label><input type="range" id="eq4k" min="-12" max="12" value="0" /></div>\n          <div class="field"><label>Air / Brilliance (12 kHz)</label><input type="range" id="eq12k" min="-12" max="12" value="0" /></div>\n        </div>\n      </div>\n    </div>\n  </div>\n\n  <!-- Bottom Glass Player Dock -->\n  <div class="glass-card player-dock">\n    <div class="now-info">\n      <img id="nowCover" src="https://via.placeholder.com/120/15171e/ffffff?text=FLAC" />\n      <div style="overflow:hidden;">\n        <div id="nowTitle" style="font-weight:700; font-size:15px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">Ready to Stream</div>\n        <div id="nowArtist" style="font-size:12px; opacity:0.7; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">Telegram Lossless Server</div>\n        <div id="nowBadge" class="hi-res-pill" style="margin-top:4px;">LOSSLESS FLAC</div>\n      </div>\n    </div>\n\n    <div class="transport">\n      <div class="ctrl-btns">\n        <button class="btn-icon" onclick="prevTrack()" title="Previous">⏮</button>\n        <button class="btn-circle" id="playPauseBtn" onclick="togglePlay()">▶</button>\n        <button class="btn-icon" onclick="nextTrack()" title="Next">⏭</button>\n        <button class="btn-icon" onclick="startSmartRadio()" title="Start Smart Radio Queue">📻 Radio</button>\n      </div>\n      <div class="seek-bar">\n        <span id="curTime">0:00</span>\n        <input type="range" id="seekSlider" min="0" max="100" value="0" oninput="seekTo(this.value)" />\n        <span id="durTime">0:00</span>\n      </div>\n    </div>\n\n    <div style="display:flex; justify-content:flex-end; align-items:center; gap:10px;">\n      <span style="font-size:12px; opacity:0.7;">🔊</span>\n      <input type="range" min="0" max="1" step="0.01" value="1" style="width:110px; accent-color:#fff;" oninput="audio.volume = this.value" />\n    </div>\n  </div>\n\n  <audio id="audioElement" crossorigin="anonymous" preload="auto"></audio>\n\n  <!-- Setup Modal -->\n  <div class="modal-backdrop" id="setupModal">\n    <div class="glass-card modal-box">\n      <h3 style="margin-bottom:10px;">Connect Telegram Lossless Vault</h3>\n      <p style="font-size:12px; opacity:0.75; margin-bottom:14px;">\n        Enter your credentials from <b>my.telegram.org</b> and your private channel ID (starting with <code>-100</code>).\n      </p>\n      <div class="field"><label>API ID</label><input type="number" id="inpApiId" placeholder="12345678" /></div>\n      <div class="field"><label>API Hash</label><input type="text" id="inpApiHash" placeholder="0123456789abcdef..." /></div>\n      <div class="field"><label>Private Channel ID</label><input type="number" id="inpChannelId" placeholder="-1001928374650" /></div>\n      <div class="field"><label>Phone Number (with country code)</label><input type="text" id="inpPhone" placeholder="+919876543210" /></div>\n      <div class="field" id="otpBox" style="display:none;"><label>Telegram Login Code (OTP)</label><input type="text" id="inpOtp" placeholder="12345" /></div>\n\n      <div style="display:flex; gap:10px; margin-top:16px;">\n        <button class="sync-btn" onclick="submitSetup()" id="setupSubmitBtn">Send Login Code</button>\n        <button class="nav-btn" onclick="closeSetupModal()" style="width:auto;">Close</button>\n      </div>\n    </div>\n  </div>\n\n  <script>\n    const audio = document.getElementById("audioElement");\n    let catalogue = [];\n    let filteredQueue = [];\n    let currentIndex = -1;\n    let currentView = "songs";\n    let syncedLyrics = [];\n    let audioCtx, sourceNode, filters = [];\n\n    async function initApp() {\n      const st = await fetch("/api/status").then(r => r.json()).catch(() => ({configured: false}));\n      if (!st.configured || !st.authorized) {\n        openSetupModal();\n      } else {\n        await loadCatalogue();\n      }\n    }\n\n    async function loadCatalogue() {\n      catalogue = await fetch("/api/catalogue").then(r => r.json());\n      document.getElementById("countSongs").innerText = catalogue.length;\n      document.getElementById("countAlbums").innerText = new Set(catalogue.map(t => t.album)).size;\n      document.getElementById("countArtists").innerText = new Set(catalogue.map(t => t.artist)).size;\n      document.getElementById("countHiRes").innerText = catalogue.filter(t => t.bit_depth >= 24).length;\n      renderCatalogue();\n    }\n\n    function setView(view, btn) {\n      currentView = view;\n      document.querySelectorAll(".nav-btn").forEach(b => b.classList.remove("active"));\n      if (btn) btn.classList.add("active");\n      renderCatalogue();\n    }\n\n    function formatTime(sec) {\n      if (!sec || isNaN(sec)) return "0:00";\n      const m = Math.floor(sec / 60);\n      const s = Math.floor(sec % 60).toString().padStart(2, "0");\n      return `${m}:${s}`;\n    }\n\n    function renderCatalogue() {\n      const q = document.getElementById("searchInput").value.toLowerCase();\n      const box = document.getElementById("catalogueContent");\n      let list = catalogue.filter(t =>\n        t.title.toLowerCase().includes(q) ||\n        t.artist.toLowerCase().includes(q) ||\n        t.album.toLowerCase().includes(q) ||\n        `${t.bit_depth}-bit`.includes(q)\n      );\n\n      if (currentView === "hires") {\n        list = list.filter(t => t.bit_depth >= 24);\n      }\n\n      filteredQueue = list;\n\n      if (currentView === "albums") {\n        const albums = {};\n        list.forEach(t => {\n          if (!albums[t.album]) albums[t.album] = t;\n        });\n        box.innerHTML = `<div class="grid-albums">` + Object.values(albums).map(a => `\n          <div class="album-card" onclick="filterByText(\'${a.album.replace(/\'/g, "\\\'")}\')">\n            <img src="/api/cover/${a.msg_id}" onerror="this.src=\'https://via.placeholder.com/200/15171e/ffffff?text=FLAC\'" />\n            <div style="font-weight:700; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${a.album}</div>\n            <div style="font-size:11px; opacity:0.65;">${a.artist}</div>\n          </div>\n        `).join("") + `</div>`;\n        return;\n      }\n\n      box.innerHTML = list.map((t, idx) => {\n        const khz = (t.sample_rate / 1000).toFixed(1).replace(".0", "");\n        return `\n          <div class="song-row ${filteredQueue[currentIndex]?.msg_id === t.msg_id ? \'active\' : \'\'}" onclick="playIndex(${idx})">\n            <img src="/api/cover/${t.msg_id}" onerror="this.src=\'https://via.placeholder.com/80/15171e/ffffff?text=♪\'" />\n            <div style="overflow:hidden;">\n              <div style="font-weight:600; font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${t.title}</div>\n              <div style="font-size:12px; opacity:0.65;">${t.artist}</div>\n            </div>\n            <div style="font-size:12px; opacity:0.65; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${t.album}</div>\n            <div class="hi-res-pill">${t.bit_depth}-BIT • ${khz}kHz</div>\n            <div style="font-size:12px; opacity:0.6; text-align:right;">${formatTime(t.duration)}</div>\n          </div>\n        `;\n      }).join("");\n    }\n\n    function filterByText(txt) {\n      document.getElementById("searchInput").value = txt;\n      setView("songs");\n    }\n\n    function playIndex(idx) {\n      if (idx < 0 || idx >= filteredQueue.length) return;\n      currentIndex = idx;\n      const t = filteredQueue[idx];\n      const khz = (t.sample_rate / 1000).toFixed(1).replace(".0", "");\n\n      document.getElementById("nowTitle").innerText = t.title;\n      document.getElementById("nowArtist").innerText = `${t.artist} — ${t.album}`;\n      document.getElementById("nowBadge").innerText = `${t.bit_depth}-BIT / ${khz}kHz LOSSLESS`;\n      const coverUrl = `/api/cover/${t.msg_id}`;\n      document.getElementById("nowCover").src = coverUrl;\n      document.getElementById("ambientBg").style.backgroundImage = `url(${coverUrl})`;\n\n      audio.src = `/api/stream/${t.msg_id}`;\n      audio.play();\n      document.getElementById("playPauseBtn").innerText = "⏸";\n      renderCatalogue();\n      loadLyrics(t.title, t.artist);\n\n      if (\'mediaSession\' in navigator) {\n        navigator.mediaSession.metadata = new MediaMetadata({\n          title: t.title, artist: t.artist, album: t.album,\n          artwork: [{ src: coverUrl, sizes: \'512x512\', type: \'image/jpeg\' }]\n        });\n      }\n    }\n\n    function togglePlay() {\n      if (!audio.src && filteredQueue.length) return playIndex(0);\n      if (audio.paused) { audio.play(); document.getElementById("playPauseBtn").innerText = "⏸"; }\n      else { audio.pause(); document.getElementById("playPauseBtn").innerText = "▶"; }\n    }\n\n    function nextTrack() { if (currentIndex + 1 < filteredQueue.length) playIndex(currentIndex + 1); }\n    function prevTrack() { if (currentIndex - 1 >= 0) playIndex(currentIndex - 1); }\n    audio.onended = () => nextTrack();\n\n    audio.ontimeupdate = () => {\n      document.getElementById("curTime").innerText = formatTime(audio.currentTime);\n      document.getElementById("durTime").innerText = formatTime(audio.duration);\n      if (audio.duration) {\n        document.getElementById("seekSlider").value = (audio.currentTime / audio.duration) * 100;\n      }\n      let activeIdx = -1;\n      for (let i = 0; i < syncedLyrics.length; i++) {\n        if (audio.currentTime >= syncedLyrics[i].time) activeIdx = i;\n      }\n      syncedLyrics.forEach((item, i) => {\n        if (i === activeIdx) {\n          item.el.classList.add("active");\n          item.el.scrollIntoView({ behavior: "smooth", block: "center" });\n        } else {\n          item.el.classList.remove("active");\n        }\n      });\n    };\n\n    function seekTo(pct) {\n      if (audio.duration) audio.currentTime = (pct / 100) * audio.duration;\n    }\n\n    async function loadLyrics(title, artist) {\n      const pane = document.getElementById("lyricsPane");\n      pane.innerHTML = `<p style="opacity:0.5;">Fetching time-synced lyrics...</p>`;\n      syncedLyrics = [];\n      try {\n        const res = await fetch(`https://lrclib.net/api/search?q=${encodeURIComponent(title + " " + artist)}`);\n        const data = await res.json();\n        const found = data.find(d => d.syncedLyrics);\n        if (!found) { pane.innerHTML = `<p style="opacity:0.5;">No synced lyrics found for this track.</p>`; return; }\n        pane.innerHTML = "";\n        found.syncedLyrics.split("\\n").forEach(line => {\n          const m = line.match(/\\[(\\d+):(\\d+\\.\\d+)\\](.*)/);\n          if (m) {\n            const time = parseInt(m[1]) * 60 + parseFloat(m[2]);\n            const text = m[3].trim();\n            if (text) {\n              const div = document.createElement("div");\n              div.className = "lyric-line";\n              div.innerText = text;\n              div.onclick = () => { audio.currentTime = time; };\n              pane.appendChild(div);\n              syncedLyrics.push({ time, el: div });\n            }\n          }\n        });\n      } catch (e) {\n        pane.innerHTML = `<p style="opacity:0.5;">Lyrics service unreachable.</p>`;\n      }\n    }\n\n    function toggleBitPerfect() {\n      const bp = document.getElementById("bitPerfectToggle").checked;\n      const ctrl = document.getElementById("dspControls");\n      if (bp) {\n        ctrl.style.opacity = "0.4";\n        ctrl.style.pointerEvents = "none";\n        filters.forEach(f => f.gain.value = 0);\n      } else {\n        ctrl.style.opacity = "1";\n        ctrl.style.pointerEvents = "auto";\n        if (!audioCtx) {\n          audioCtx = new (window.AudioContext || window.webkitAudioContext)();\n          sourceNode = audioCtx.createMediaElementSource(audio);\n          const freqs = [60, 250, 1000, 4000, 12000];\n          const ids = ["eq60", "eq250", "eq1k", "eq4k", "eq12k"];\n          let prev = sourceNode;\n          freqs.forEach((f, idx) => {\n            const node = audioCtx.createBiquadFilter();\n            node.type = idx === 0 ? "lowshelf" : idx === 4 ? "highshelf" : "peaking";\n            node.frequency.value = f;\n            prev.connect(node);\n            prev = node;\n            filters.push(node);\n            document.getElementById(ids[idx]).oninput = e => node.gain.value = parseFloat(e.target.value);\n          });\n          prev.connect(audioCtx.destination);\n        }\n      }\n    }\n\n    function startSmartRadio() {\n      if (currentIndex < 0) return;\n      const seed = filteredQueue[currentIndex];\n      // Smart Radio: prioritizes same genre/artist or similar bit-depth lossless tracks\n      const radioQueue = [seed, ...catalogue.filter(t => t.msg_id !== seed.msg_id).sort((a, b) => {\n        const scoreA = (a.artist === seed.artist ? 3 : 0) + (a.genre === seed.genre ? 2 : 0) + Math.random();\n        const scoreB = (b.artist === seed.artist ? 3 : 0) + (b.genre === seed.genre ? 2 : 0) + Math.random();\n        return scoreB - scoreA;\n      })];\n      filteredQueue = radioQueue;\n      currentIndex = 0;\n      renderCatalogue();\n    }\n\n    function switchRightTab(tab) {\n      document.getElementById("lyricsPane").style.display = tab === "lyrics" ? "block" : "none";\n      document.getElementById("eqPane").style.display = tab === "eq" ? "block" : "none";\n      document.getElementById("tabLyricsBtn").classList.toggle("active", tab === "lyrics");\n      document.getElementById("tabEqBtn").classList.toggle("active", tab === "eq");\n    }\n\n    async function syncTelegram() {\n      const btn = document.getElementById("syncBtn");\n      btn.innerText = "⏳ Indexing FLAC Headers...";\n      const res = await fetch("/api/sync", { method: "POST" });\n      if (res.ok) {\n        await loadCatalogue();\n        btn.innerText = "✓ Vault Synced";\n        setTimeout(() => btn.innerText = "↻ Sync Telegram Vault", 2500);\n      } else {\n        btn.innerText = "⚠ Auth Required";\n        openSetupModal();\n      }\n    }\n\n    function openSetupModal() { document.getElementById("setupModal").style.display = "flex"; }\n    function closeSetupModal() { document.getElementById("setupModal").style.display = "none"; }\n\n    let awaitingOtp = false;\n    async function submitSetup() {\n      const api_id = parseInt(document.getElementById("inpApiId").value);\n      const api_hash = document.getElementById("inpApiHash").value.trim();\n      const channel_id = parseInt(document.getElementById("inpChannelId").value);\n      const phone = document.getElementById("inpPhone").value.trim();\n\n      if (!awaitingOtp) {\n        const res = await fetch("/api/auth/send_code", {\n          method: "POST", headers: { "Content-Type": "application/json" },\n          body: JSON.stringify({ api_id, api_hash, channel_id, phone })\n        }).then(r => r.json());\n        if (res.status === "code_sent") {\n          awaitingOtp = true;\n          document.getElementById("otpBox").style.display = "block";\n          document.getElementById("setupSubmitBtn").innerText = "Verify OTP & Connect";\n        } else if (res.status === "already_authorized") {\n          closeSetupModal();\n          syncTelegram();\n        }\n      } else {\n        const code = document.getElementById("inpOtp").value.trim();\n        const res = await fetch("/api/auth/verify", {\n          method: "POST", headers: { "Content-Type": "application/json" },\n          body: JSON.stringify({ phone, code })\n        });\n        if (res.ok) {\n          closeSetupModal();\n          syncTelegram();\n        }\n      }\n    }\n\n    initApp();\n  </script>\n</body>\n</html>\n'
MANIFEST_JSON = '{\n  "name": "Aether Lossless Music",\n  "short_name": "Aether",\n  "description": "Bit-Perfect 24-Bit Lossless Music Player powered by Telegram Cloud",\n  "start_url": "/",\n  "display": "standalone",\n  "background_color": "#07080c",\n  "theme_color": "#07080c",\n  "icons": [\n    {\n      "src": "https://cdn-icons-png.flaticon.com/512/3844/3844724.png",\n      "sizes": "512x512",\n      "type": "image/png"\n    }\n  ]\n}\n'


@app.get("/manifest.json")
async def serve_manifest():
    return Response(content=MANIFEST_JSON, media_type="application/json")

@app.get("/player")
async def serve_player():
    return HTMLResponse(content=INDEX_HTML)

@app.get("/")
async def serve_root():
    return HTMLResponse(content=INDEX_HTML)

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
