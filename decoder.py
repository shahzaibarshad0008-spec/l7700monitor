import re
from typing import Optional, Dict

def _parse_timestamp(ts_bytes: bytes) -> str:
    """Parse the 8-byte timestamp block"""
    if len(ts_bytes) < 6:
        return "0000-00-00 00:00:00"

    yy, mm, dd, hh, mi, ss = ts_bytes[0], ts_bytes[1], ts_bytes[2], ts_bytes[3], ts_bytes[4], ts_bytes[5]
    year = 2000 + yy if yy < 100 else yy
    return f"{year:04}-{mm:02}-{dd:02} {hh:02}:{mi:02}:{ss:02}"

def _cleanup_ascii_block(s: str) -> str:
    """Strip nulls and extra spaces"""
    return s.replace("\x00", "").strip()

def decode_l7700_packet(data: bytes) -> Optional[Dict]:
    """Decode an L7700 UDP packet"""
    try:
        if not data or data[0] != 0x02 or data[-1] != 0x03:
            return None

        payload = data[1:-1]

        if len(payload) < 24:
            return None

        ts_block = payload[16:24]
        timestamp = _parse_timestamp(ts_block)

        ascii_payload = payload.decode("latin1", "ignore")

        # Room detection
        room = "UNKNOWN"
        room_match = re.search(r"([A-Za-z0-9\s\-]{1,40}ROOM\s*\d{0,4})", ascii_payload, flags=re.IGNORECASE)
        if room_match:
            room = _cleanup_ascii_block(room_match.group(1))
        else:
            alt_match = re.search(r"([A-Za-z0-9\-\s]{5,40})", ascii_payload)
            if alt_match:
                room = _cleanup_ascii_block(alt_match.group(1))

        # Device type
        device = "UNKNOWN"
        dev_idx = ascii_payload.find("INTERCALL-IP")
        if dev_idx != -1:
            device = "INTERCALL-IP"
        else:
            dev_alt = re.search(r"([A-Z0-9\-\_]{6,20})", ascii_payload)
            if dev_alt:
                device = _cleanup_ascii_block(dev_alt.group(1))

        # Event
        event = "UNKNOWN"
        if dev_idx != -1:
            start = dev_idx + len("INTERCALL-IP")
            event_block = ascii_payload[start:start + 32]
            event = _cleanup_ascii_block(event_block)
            if event:
                event = event.split()[0]
        else:
            for candidate in ("Call", "Accept", "Cancel", "Presence", "Reset", "ROOM SERVICE", "Doctor Present",
                               "Present", "CATERING SERVICE", "Alarm", "Assistance", "LowBattery", "Emergency", "Isolate"):
                if candidate.lower() in ascii_payload.lower():
                    event = candidate
                    break

        return {
            "timestamp": timestamp,
            "room": room.strip(),
            "device": device.strip(),
            "event": event.strip(),
            "raw_hex": data.hex(),
        }
    except Exception:
        return None
