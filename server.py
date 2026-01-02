import socket
import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import quote
import base64

import uvicorn
import pymysql
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import joinedload
from sqlalchemy import inspect

from decoder import decode_l7700_packet
from camera_stream import CameraManager
from config import Config
from models import (
    get_db_engine, get_db_session, init_database,
    Floor, Ward, Room, Bed, Camera, Event, ColorScheme, CallSession,
    get_karachi_time
)

# ----------------- Globals -----------------

engine = get_db_engine(Config.DATABASE_URL)
camera_manager = CameraManager(use_simulation=False)
active_connections = {}  # ws clients


# ----------------- Helpers -----------------

def _norm(s: str) -> str:
    if not s:
        return ""
    return "".join(ch for ch in s.upper() if ch.isalnum())


def resolve_bed_in_room(session, room: Room, decoded: dict):
    if not room:
        return None

    beds = session.query(Bed).filter(Bed.room_id == room.id).all()
    if not beds:
        return None

    tokens = []
    for key in ("bed", "device", "room"):
        v = (decoded.get(key) or "").strip()
        if v:
            tokens.append(v)

    # 1) exact match
    for t in tokens:
        tl = t.lower()
        for b in beds:
            if b.bed_number and b.bed_number.strip().lower() == tl:
                return b
            if b.bed_name and b.bed_name.strip().lower() == tl:
                return b

    # 2) normalized partial match
    ntokens = [_norm(t) for t in tokens if t]
    for b in beds:
        bn = _norm(b.bed_number or "")
        bname = _norm(b.bed_name or "")
        for nt in ntokens:
            if not nt:
                continue
            if (nt in bname) or (nt in bn) or (bname and bname in nt) or (bn and bn in nt):
                return b

    # 3) numeric hint
    joined = " ".join(tokens)
    nums = re.findall(r"\b\d+\b", joined)
    if nums:
        for n in nums:
            for b in beds:
                if (b.bed_number or "").strip() == n:
                    return b

    # 4) fallback
    if len(beds) == 1:
        return beds[0]
    return sorted(beds, key=lambda x: x.id)[0]


def get_or_create_call_session(session, bed_id, event_type):
    cs = session.query(CallSession).filter(
        CallSession.bed_id == bed_id,
        CallSession.status == "active"
    ).first()

    if cs:
        cs.current_event_type = event_type
        session.commit()
        return cs

    cs = CallSession(
        bed_id=bed_id,
        current_event_type=event_type,
        status="active"
    )
    session.add(cs)
    session.commit()
    return cs


def init_database_tables():
    try:
        socket_path = Config.DB_SOCKET or "/opt/lampp/var/mysql/mysql.sock"
        print(f"[v0] Using MySQL socket: {socket_path}")
        print(f"[v0] Using DB: {Config.DB_NAME}")

        connection = pymysql.connect(
            unix_socket=socket_path,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            charset="utf8mb4",
        )
        cursor = connection.cursor()
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{Config.DB_NAME}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cursor.close()
        connection.close()

        init_database(engine)
        print("[v0] Database and tables initialized successfully")

        inspector = inspect(engine)
        tables = inspector.get_table_names()
        print(f"[v0] Tables: {', '.join(tables)}")

    except Exception as e:
        print(f"[v0] Error initializing database: {e}")
        raise


def get_room_camera(session, room: Room):
    if not room:
        return None
    return session.query(Camera).filter(
        Camera.room_id == room.id,
        Camera.status == "active"
    ).order_by(Camera.id.desc()).first()


def _room_key(name: str) -> str:
    return (name or "").strip()


def serialize_event_with_camera(session, event: Event):
    bed = event.bed
    room = event.room or (bed.room if bed else None)
    ward = room.ward if room else None
    floor = ward.floor if ward else None

    cam = get_room_camera(session, room)

    room_name = _room_key(room.room_name if room else None)

    streaming_rooms = set(camera_manager.get_all_rooms())
    camera_streaming = bool(room_name) and (room_name in streaming_rooms)
    camera_live = bool(room_name) and camera_manager.has_frame(room_name)

    cam_url = f"/camera/{quote(room_name, safe='')}" if camera_streaming else None

    ts = event.system_timestamp
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S") if ts else None

    return {
        "id": event.id,
        "call_session_id": event.call_session_id,
        "event_type": event.event_type,
        "event": event.event_type,
        "status": event.status,
        "system_timestamp": ts_str,

        "bed_id": bed.id if bed else None,
        "bed_name": bed.bed_name if bed else None,
        "bed_number": bed.bed_number if bed else None,

        "room_id": room.id if room else None,
        "room_name": room_name or None,
        "room_number": room.room_number if room else None,

        "ward_name": ward.name if ward else None,
        "floor_name": floor.name if floor else None,

        "camera_available": camera_streaming,   # stream thread exists
        "camera_live": camera_live,             # frames are actually coming
        "camera_url": cam_url,

        "camera_configured": True if cam else False,
    }


def mjpeg_generator(room_name: str, fps: int = 10):
    room_name = _room_key(room_name)
    frame_delay = 1.0 / max(1, int(fps))

    last_placeholder = 0.0

    while True:
        frame = camera_manager.get_frame(room_name)

        if frame:
            payload = frame
            time.sleep(frame_delay)
        else:
            # Send placeholder at least once per second so <img> doesn't stay blank
            now = time.time()
            if now - last_placeholder < 1.0:
                time.sleep(0.05)
                continue
            last_placeholder = now
            payload = _PLACEHOLDER_JPEG

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Cache-Control: no-store\r\n"
            b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" +
            payload + b"\r\n"
        )

# Small placeholder JPEG bytes (sent when no camera frame yet)
_PLACEHOLDER_JPEG = base64.b64decode(
    b"/9j/4AAQSkZJRgABAQAAAQABAAD/2wCEAAkGBxAQEBUQEA8QFQ8QDw8QDw8PDw8PFRUWFhUVFRUY"
    b"HSggGBolGxUVITEhJSkrLi4uFx8zODMsNygtLisBCgoKDg0OGxAQGy0lICUtLS0tLS0tLS0tLS0t"
    b"LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLf/AABEIAH4A5QMBIgACEQEDEQH/xAAX"
    b"AAEBAQEAAAAAAAAAAAAAAAAAAQID/8QAHhAAAQQCAwEAAAAAAAAAAAAAAQACAxESITFBYXH/xAAX"
    b"AQEBAQEAAAAAAAAAAAAAAAAAAQID/8QAHBEBAAICAwEAAAAAAAAAAAAAAAECEQMhEjFB/9oADAMB"
    b"AAIRAxEAPwDqkYq0Gm1l8p3Wqkq6zq0gK1kQmQp7j2p0o3bGx1m2m1yYq1pWkq0c1QmGmQ+9T9m"
    b"j6yN2s2j2yqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkq"
    b"gqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqk"
    b"qgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgq"
    b"kqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqgqkqg//9k="
)

# ----------------- Lifespan -----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database_tables()

    # Load cameras from DB and start streams
    session = get_db_session(engine)
    try:
        cameras = session.query(Camera).options(joinedload(Camera.room)).filter(Camera.status == "active").all()
        for cam in cameras:
            if cam.room and cam.rtsp_url:
                try:
                    camera_manager.add_camera(cam.room.room_name, cam.rtsp_url)
                except Exception as e:
                    print(f"[Camera] Failed to start camera id={cam.id}: {e}")
    finally:
        session.close()

    asyncio.create_task(udp_listener())
    yield
    camera_manager.shutdown()


app = FastAPI(lifespan=lifespan)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")


# ----------------- Pages -----------------

@app.get("/")
async def get_home():
    with open("templates/dashboard.html", "r") as f:
        return HTMLResponse(content=f.read())


@app.get("/calls")
async def get_calls_page():
    with open("templates/calls.html", "r") as f:
        return HTMLResponse(content=f.read())


@app.get("/history")
async def get_history_page():
    with open("templates/history.html", "r") as f:
        return HTMLResponse(content=f.read())


@app.get("/config")
async def get_config_page():
    with open("templates/config.html", "r") as f:
        return HTMLResponse(content=f.read())


# ----------------- Camera Streams -----------------

@app.head("/video_feed/{camera_id}")
async def video_feed_head(camera_id: int):
    return Response(status_code=200)


@app.get("/video_feed/{camera_id}")
async def video_feed(camera_id: int):
    # kept for compatibility
    session = get_db_session(engine)
    try:
        cam = session.query(Camera).options(joinedload(Camera.room)).filter(Camera.id == camera_id).first()
        if not cam or not cam.room:
            return JSONResponse({"error": "Camera not found"}, status_code=404)
        room_name = _room_key(cam.room.room_name)
    finally:
        session.close()

    if room_name not in camera_manager.get_all_rooms():
        return JSONResponse({"error": f"No active camera stream for room: {room_name}"}, status_code=404)

    return StreamingResponse(
        mjpeg_generator(room_name, fps=10),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
                 "Cache-Control": "no-store",
                 "Pragma": "no-cache",
                 "X-Accel-Buffering": "no"
                }    

    )


@app.head("/camera/{room_name:path}")
async def camera_by_room_head(room_name: str):
    return Response(status_code=200)


@app.get("/camera/{room_name:path}")
async def camera_by_room(room_name: str):
    room_name = _room_key(room_name)

    if room_name not in camera_manager.get_all_rooms():
        return JSONResponse({"error": f"No active camera stream for room: {room_name}"}, status_code=404)

    return StreamingResponse(
        mjpeg_generator(room_name, fps=10),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
                 "Cache-Control": "no-store",
                 "Pragma": "no-cache",
                 "X-Accel-Buffering": "no"
            }

    )


# ----------------- APIs -----------------

@app.get("/api/stats")
async def get_stats():
    session = get_db_session(engine)
    try:
        seven_days_ago = datetime.now() - timedelta(days=7)
        one_hour_ago = datetime.now() - timedelta(hours=1)

        total_active = session.query(Event).filter(
            Event.created_at >= seven_days_ago,
            Event.status == "active"
        ).count()

        urgent_alarms = session.query(Event).filter(
            Event.created_at >= seven_days_ago,
            Event.event_type.in_(("Emergency", "Alarm", "Assistance")),
            Event.status == "active"
        ).count()

        ongoing_calls = session.query(Event).filter(
            Event.created_at >= seven_days_ago,
            Event.event_type == "Call",
            Event.status == "active"
        ).count()

        recently_cleared = session.query(Event).filter(
            Event.created_at >= one_hour_ago,
            Event.status == "cleared"
        ).count()

        return {
            "total_active": total_active,
            "urgent_alarms": urgent_alarms,
            "ongoing_calls": ongoing_calls,
            "recently_cleared": recently_cleared
        }
    finally:
        session.close()


@app.get("/api/events/recent")
async def get_recent_events(limit: int = 100):
    session = get_db_session(engine)
    try:
        events = session.query(Event).options(
            joinedload(Event.bed).joinedload(Bed.room).joinedload(Room.ward).joinedload(Ward.floor),
            joinedload(Event.room).joinedload(Room.ward).joinedload(Ward.floor),
        ).order_by(Event.id.desc()).limit(limit).all()

        return [serialize_event_with_camera(session, e) for e in events]
    finally:
        session.close()


@app.get("/api/cameras")
async def get_cameras():
    session = get_db_session(engine)
    try:
        cams = session.query(Camera).options(joinedload(Camera.room)).all()
        return {
            "cameras": [{
                "id": c.id,
                "room_name": c.room.room_name if c.room else None,
                "rtsp_url": c.rtsp_url,
                "status": c.status
            } for c in cams],
            "rooms": camera_manager.get_all_rooms()
        }
    finally:
        session.close()


# ---- Config GET endpoints ----

@app.get("/api/config/floors")
async def get_floors():
    session = get_db_session(engine)
    try:
        floors = session.query(Floor).all()
        return {"floors": [{"id": f.id, "name": f.name} for f in floors]}
    finally:
        session.close()


@app.get("/api/config/wards")
async def get_wards():
    session = get_db_session(engine)
    try:
        wards = session.query(Ward).all()
        return {"wards": [{"id": w.id, "name": w.name, "floor_id": w.floor_id} for w in wards]}
    finally:
        session.close()


@app.get("/api/config/rooms")
async def get_rooms():
    session = get_db_session(engine)
    try:
        rooms = session.query(Room).all()
        return {"rooms": [{
            "id": r.id,
            "room_name": r.room_name,
            "room_number": r.room_number,
            "ward_id": r.ward_id,
            "system_ip": getattr(r, "system_ip", None),
            "status": getattr(r, "status", None),
        } for r in rooms]}
    finally:
        session.close()


@app.get("/api/config/beds")
async def get_beds():
    session = get_db_session(engine)
    try:
        beds = session.query(Bed).all()
        return {"beds": [{
            "id": b.id,
            "room_id": b.room_id,
            "bed_name": b.bed_name,
            "bed_number": b.bed_number,
            "camera_id": getattr(b, "camera_id", None),
            "status": getattr(b, "status", None),
        } for b in beds]}
    finally:
        session.close()


@app.get("/api/config/colors")
async def get_colors():
    session = get_db_session(engine)
    try:
        colors = session.query(ColorScheme).all()
        return {"colors": [{"id": c.id, "event_type": c.event_type, "color": c.color} for c in colors]}
    finally:
        session.close()


# ----------------- WebSocket -----------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    client_id = id(ws)
    active_connections[client_id] = ws
    try:
        while True:
            # keep connection alive; client doesn’t need to send anything
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        active_connections.pop(client_id, None)


# ----------------- UDP Listener -----------------

async def udp_listener():
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        sock.bind((Config.UDP_IP, Config.UDP_PORT))
        print(f"✓ UDP listener bound to {Config.UDP_IP}:{Config.UDP_PORT}")
    except OSError as e:
        print(f"✗ Error binding UDP {Config.UDP_PORT}: {e}")
        return

    print(f"🎧 Listening for L7700 UDP on {Config.UDP_IP}:{Config.UDP_PORT}")
    print(f"⏰ {get_karachi_time().strftime('%Y-%m-%d %H:%M:%S')}")

    while True:
        try:
            data, addr = await loop.run_in_executor(None, sock.recvfrom, 2048)
            incoming_ip = addr[0]

            decoded = decode_l7700_packet(data)
            if not decoded:
                continue

            session = get_db_session(engine)
            try:
                room = session.query(Room).filter(Room.system_ip == incoming_ip).first()
                bed = resolve_bed_in_room(session, room, decoded) if room else None

                system_time = get_karachi_time()
                event_type = decoded.get("event") or "Unknown"

                room_id = room.id if room else None
                bed_id = bed.id if bed else None

                # reset ends session
                if event_type.lower() == "reset" and bed_id:
                    cs = session.query(CallSession).filter(
                        CallSession.bed_id == bed_id,
                        CallSession.status == "active"
                    ).first()
                    if cs:
                        cs.status = "ended"
                        cs.ended_at = system_time
                        session.commit()

                call_session_id = None
                if event_type.lower() != "reset" and bed_id:
                    cs = get_or_create_call_session(session, bed_id, event_type)
                    call_session_id = cs.id

                event = Event(
                    room_id=room_id,
                    bed_id=bed_id,
                    call_session_id=call_session_id,
                    device_timestamp=decoded.get("timestamp"),
                    system_timestamp=system_time,
                    room_identifier=decoded.get("room"),
                    device_type=decoded.get("device"),
                    event_type=event_type,
                    status="active",
                    raw_hex=decoded.get("raw_hex")
                )
                session.add(event)
                session.commit()

                payload = serialize_event_with_camera(session, event)
                message = json.dumps(payload)

                dead = []
                for cid, w in active_connections.items():
                    try:
                        await w.send_text(message)
                    except Exception:
                        dead.append(cid)
                for cid in dead:
                    active_connections.pop(cid, None)

            except Exception as e:
                session.rollback()
                print(f"✗ UDP/DB error: {e}")
            finally:
                session.close()

        except Exception as e:
            print(f"✗ UDP loop error: {e}")


if __name__ == "__main__":
    # Cameras + OpenCV are unstable with reload=True. Keep reload OFF.
    uvicorn.run("server:app", host=Config.SERVER_HOST, port=Config.SERVER_PORT, reload=False)
