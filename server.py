import socket
import asyncio
import json
import uvicorn
import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, Body, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from datetime import datetime, timedelta
import pymysql

from sqlalchemy import or_
from decoder import decode_l7700_packet
from camera_stream import CameraManager
from onvif_discovery import ONVIFCameraDiscovery
from config import Config
from models import (
    get_db_engine, get_db_session, init_database,
    Floor, Ward, Room, Bed, Camera, Event, ColorScheme, CallSession,
    get_karachi_time
)

# ----------------- Helpers -----------------

def _norm(s: str) -> str:
    if not s:
        return ""
    return "".join(ch for ch in s.upper() if ch.isalnum())

def resolve_bed_in_room(session, room: Room, decoded: dict):
    """
    Best-effort bed matching inside a room.
    Tries device/room strings against bed_name/bed_number.
    If still not found:
      - if only 1 bed -> return it
      - else fallback -> smallest id bed (so call shows with some bed_name)
    """
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

    # 3) numeric hint (if packet contains "1", "2", etc)
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

    beds_sorted = sorted(beds, key=lambda x: x.id)
    return beds_sorted[0]  # fallback so call is visible with bed_name

def get_or_create_call_session(session, bed_id, event_type):
    call_session = session.query(CallSession).filter(
        CallSession.bed_id == bed_id,
        CallSession.status == 'active'
    ).first()

    if call_session:
        call_session.current_event_type = event_type
        session.commit()
        return call_session

    call_session = CallSession(
        bed_id=bed_id,
        current_event_type=event_type,
        status='active'
    )
    session.add(call_session)
    session.commit()
    return call_session

# ----------------- App Lifespan -----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database_tables()

    # Load cameras from database
    session = get_db_session(engine)
    try:
        cameras = session.query(Camera).filter(Camera.status == 'active').all()
        for camera in cameras:
            try:
                if camera.room:
                    camera_manager.add_camera(f"{camera.room.room_name}", camera.rtsp_url)
            except Exception as e:
                print(f"Failed to start camera {camera.camera_name}: {e}")
    finally:
        session.close()

    asyncio.create_task(udp_listener())
    yield
    camera_manager.shutdown()

app = FastAPI(lifespan=lifespan)
active_connections = {}
camera_manager = CameraManager(use_simulation=False)

engine = get_db_engine(Config.DATABASE_URL)

def init_database_tables():
    try:
        connection = pymysql.connect(
            host=Config.DB_HOST,
            port=int(Config.DB_PORT),
            user=Config.DB_USER,
            password=Config.DB_PASSWORD
        )
        cursor = connection.cursor()
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS {Config.DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cursor.close()
        connection.close()

        init_database(engine)
        print("[v0] Database and tables initialized successfully")

        from sqlalchemy import inspect
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        print(f"[v0] Created tables: {', '.join(tables)}")
    except Exception as e:
        print(f"[v0] Error initializing database: {e}")
        raise

def serialize_event(event: Event):
    room = event.room or (event.bed.room if event.bed else None)

    return {
        'id': event.id,
        'event_type': event.event_type,
        'event': event.event_type,
        'status': event.status,
        'system_timestamp': event.system_timestamp.strftime('%Y-%m-%d %H:%M:%S'),
        'room_identifier': event.room_identifier,
        'device_type': event.device_type,
        'bed_id': event.bed_id,

        'bed_name': event.bed.bed_name if event.bed else None,
        'bed_number': event.bed.bed_number if event.bed else None,

        'room': room.room_name if room else None,
        'room_name': room.room_name if room else None,
        'ward_name': room.ward.name if (room and room.ward) else None,
        'floor_name': room.ward.floor.name if (room and room.ward and room.ward.floor) else None,

        'camera_url': f"/video_feed/{event.bed.camera.id}" if (event.bed and event.bed.camera) else None,
        'camera_available': True if (event.bed and event.bed.camera) else False
    }

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get():
    with open("templates/dashboard.html", "r") as f:
        return HTMLResponse(content=f.read())

@app.get("/calls")
async def get_calls():
    with open("templates/calls.html", "r") as f:
        return HTMLResponse(content=f.read())

@app.get("/history")
async def get_history():
    with open("templates/history.html", "r") as f:
        return HTMLResponse(content=f.read())

@app.get("/config")
async def get_config():
    with open("templates/config.html", "r") as f:
        return HTMLResponse(content=f.read())

# ----------------- APIs -----------------

@app.get("/api/stats")
async def get_stats():
    session = get_db_session(engine)
    try:
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

        total_active = session.query(Event).filter(
            Event.created_at >= seven_days_ago, Event.status == 'active'
        ).count()

        urgent_alarms = session.query(Event).filter(
            Event.created_at >= seven_days_ago,
            Event.event_type.in_(('Emergency', 'Alarm', 'Assistance')),
            Event.status == 'active'
        ).count()

        ongoing_calls = session.query(Event).filter(
            Event.created_at >= seven_days_ago,
            Event.event_type == 'Call',
            Event.status == 'active'
        ).count()

        one_hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        recently_cleared = session.query(Event).filter(
            Event.created_at >= one_hour_ago, Event.status == 'cleared'
        ).count()

        return {
            "total_active": total_active,
            "urgent_alarms": urgent_alarms,
            "ongoing_calls": ongoing_calls,
            "recently_cleared": recently_cleared
        }
    finally:
        session.close()

@app.get("/api/timeline")
async def get_timeline(days: int = 7):
    session = get_db_session(engine)
    try:
        days_ago = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        results = session.query(Event.event_type, Event.created_at).filter(Event.created_at >= days_ago).all()

        timeline_data = {}
        for event_type, created_at in results:
            date = created_at.strftime("%Y-%m-%d")
            timeline_data.setdefault(date, {})
            timeline_data[date][event_type] = timeline_data[date].get(event_type, 0) + 1

        return {"timeline": timeline_data}
    finally:
        session.close()

@app.get("/api/events/recent")
async def get_recent_events():
    session = get_db_session(engine)
    try:
        from sqlalchemy.orm import joinedload

        call_sessions = session.query(CallSession).options(
            joinedload(CallSession.bed).joinedload(Bed.room).joinedload(Room.ward).joinedload(Ward.floor),
            joinedload(CallSession.bed).joinedload(Bed.camera)
        ).filter(
            CallSession.status == 'active'
        ).order_by(CallSession.started_at.desc()).all()

        result = []
        for cs in call_sessions:
            bed = cs.bed
            if not bed or not bed.room:
                continue

            room = bed.room
            ward = room.ward
            floor = ward.floor if ward else None

            result.append({
                'id': cs.id,
                'event_id': cs.id,
                'call_session_id': cs.id,
                'event_type': cs.current_event_type,
                'status': 'active',

                'bed_id': bed.id,
                'bed_name': bed.bed_name,
                'bed_number': bed.bed_number,

                'room_id': room.id,
                'room_name': room.room_name,
                'room_number': room.room_number,

                'ward_id': ward.id if ward else None,
                'ward_name': ward.name if ward else None,

                'floor_id': floor.id if floor else None,
                'floor_name': floor.name if floor else None,

                'device_name': bed.bed_name,  # ✅ show bed name
                'timestamp': cs.started_at.strftime('%H:%M:%S'),
                'system_timestamp': cs.started_at.strftime('%Y-%m-%d %H:%M:%S'),
                'camera_available': True if bed.camera_id else False
            })

        return result
    finally:
        session.close()

@app.get("/camera/{room_name}")
async def camera_feed(room_name: str):
    def generate():
        while True:
            frame = camera_manager.get_frame(room_name)
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                import time
                time.sleep(0.1)

    return StreamingResponse(generate(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/api/cameras")
async def get_cameras():
    session = get_db_session(engine)
    try:
        cameras = session.query(Camera).all()
        return {
            "cameras": [{
                "id": c.id,
                "room_name": c.room.room_name if c.room else None,
                "rtsp_url": c.rtsp_url,
                "status": c.status
            } for c in cameras],
            "rooms": camera_manager.get_all_rooms()
        }
    finally:
        session.close()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    client_id = id(ws)
    active_connections[client_id] = ws
    try:
        while True:
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

            print(f"\n📦 UDP from {addr[0]}:{addr[1]} ({len(data)} bytes)")
            print(f"   Raw hex: {data.hex()[:120]}...")

            decoded = decode_l7700_packet(data)
            print(f"DECODED >>> {decoded}")

            if not decoded:
                print("✗ decode failed")
                continue

            session = get_db_session(engine)
            try:
                # 1) room by IP
                room = session.query(Room).filter(Room.system_ip == incoming_ip).first()
                if room:
                    print(f"✓ Room matched by IP {incoming_ip}: {room.room_name} (id={room.id})")
                else:
                    print(f"⚠ No room for IP {incoming_ip}")

                # 2) bed inside room (best-effort)
                bed = resolve_bed_in_room(session, room, decoded) if room else None
                if bed:
                    print(f"✓ Bed matched: {bed.bed_name} (id={bed.id})")
                else:
                    print(f"⚠ No bed matched (ip={incoming_ip}, device='{decoded.get('device')}', room='{decoded.get('room')}')")

                system_time = get_karachi_time()
                event_type = decoded.get('event') or "Unknown"

                room_id = room.id if room else None
                bed_id = bed.id if bed else None

                # reset ends session (needs bed_id)
                if event_type.lower() == 'reset' and bed_id:
                    cs = session.query(CallSession).filter(
                        CallSession.bed_id == bed_id,
                        CallSession.status == 'active'
                    ).first()
                    if cs:
                        cs.status = 'ended'
                        cs.ended_at = system_time
                        session.commit()
                        print(f"✓ Call session ended (bed_id={bed_id})")

                # create session for non-reset (needs bed_id)
                call_session_id = None
                if event_type.lower() != 'reset' and bed_id:
                    cs = get_or_create_call_session(session, bed_id, event_type)
                    call_session_id = cs.id

                # save event
                event = Event(
                    room_id=room_id,
                    bed_id=bed_id,
                    call_session_id=call_session_id,
                    device_timestamp=decoded.get('timestamp'),
                    system_timestamp=system_time,
                    room_identifier=decoded.get('room'),
                    device_type=decoded.get('device'),
                    event_type=event_type,
                    status='active',
                    raw_hex=decoded.get('raw_hex')
                )
                session.add(event)
                session.commit()

                # ws payload
                ws_room = room or (bed.room if bed else None)
                device_name = bed.bed_name if bed else (ws_room.room_name if ws_room else None)

                ws_message = {
                    'id': event.id,
                    'call_session_id': call_session_id,
                    'room': decoded.get('room'),
                    'device': decoded.get('device'),
                    'event': event_type,
                    'timestamp': decoded.get('timestamp'),
                    'system_timestamp': system_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'status': 'active',

                    'device_name': device_name,  # ✅ bed_name preferred

                    'room_id': ws_room.id if ws_room else None,
                    'room_name': ws_room.room_name if ws_room else None,
                    'room_number': ws_room.room_number if ws_room else None,
                    'ward_name': ws_room.ward.name if (ws_room and ws_room.ward) else None,
                    'floor_name': ws_room.ward.floor.name if (ws_room and ws_room.ward and ws_room.ward.floor) else None,

                    'bed_id': bed.id if bed else None,
                    'bed_name': bed.bed_name if bed else None,
                    'bed_number': bed.bed_number if bed else None,
                }

                message = json.dumps(ws_message)
                dead = []
                for cid, ws in active_connections.items():
                    try:
                        await ws.send_text(message)
                    except Exception as e:
                        print(f"WS send error: {e}")
                        dead.append(cid)
                for cid in dead:
                    active_connections.pop(cid, None)

            except Exception as e:
                print(f"✗ DB save error: {e}")
                import traceback
                traceback.print_exc()
                session.rollback()
            finally:
                session.close()

        except Exception as e:
            print(f"✗ UDP loop error: {e}")
            import traceback
            traceback.print_exc()

# ---------------- Cameras ----------------

@app.post("/api/cameras/discover")
async def discover_camera(camera_data: dict):
    ip = camera_data.get("ip")
    port = camera_data.get("port", 80)
    username = camera_data.get("username")
    password = camera_data.get("password")

    if not all([ip, username, password]):
        return {"success": False, "error": "IP, username, and password are required"}

    try:
        discovery = ONVIFCameraDiscovery(ip, port, username, password)
        if not discovery.connect():
            return {"success": False, "error": "Failed to connect via ONVIF"}

        device_info = discovery.get_device_info()
        rtsp_streams = discovery.get_rtsp_urls()
        if not rtsp_streams:
            return {"success": False, "error": "No RTSP streams found"}

        return {"success": True, "device_info": device_info, "streams": rtsp_streams}
    except Exception as e:
        return {"success": False, "error": f"Discovery failed: {str(e)}"}

@app.post("/api/cameras/add")
async def add_camera(camera_data: dict):
    room_name = camera_data.get("room_name")
    rtsp_url = camera_data.get("rtsp_url", "")

    if not room_name:
        return {"success": False, "error": "Room name is required"}

    session = get_db_session(engine)
    try:
        room = session.query(Room).filter(Room.room_name == room_name).first()
        if not room:
            return {"success": False, "error": "Room not found"}

        camera = Camera(
            room_id=room.id,
            camera_name=camera_data.get("camera_name") or f"Camera - {room_name}",
            rtsp_url=rtsp_url,
            ip_address=camera_data.get("ip_address"),
            username=camera_data.get("username"),
            password=camera_data.get("password"),
            port=camera_data.get("port", 554),
            status="active"
        )
        session.add(camera)
        session.commit()

        camera_manager.add_camera(room_name, rtsp_url)
        return {"success": True, "message": f"Camera added for {room_name}", "camera_id": camera.id}
    except Exception as e:
        session.rollback()
        return {"success": False, "error": str(e)}
    finally:
        session.close()

@app.post("/api/cameras/remove")
async def remove_camera(camera_data: dict):
    room_name = camera_data.get("room_name")
    if not room_name:
        return {"success": False, "error": "Room name is required"}

    session = get_db_session(engine)
    try:
        room = session.query(Room).filter(Room.room_name == room_name).first()
        if not room:
            return {"success": False, "error": "Room not found"}

        camera = session.query(Camera).filter(Camera.room_id == room.id).first()
        if camera:
            session.delete(camera)
            session.commit()
            camera_manager.remove_camera(room_name)

        return {"success": True, "message": f"Camera removed for {room_name}"}
    finally:
        session.close()

# ---- (baqi config endpoints aapke same rehenge; agar chaho to main full config block bhi merge kar dunga) ----

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=9000, reload=True)
