import atexit
import cv2
import numpy as np
from datetime import datetime
import threading
from typing import Dict, Optional, Union
import time

# Helps stability with OpenCV in threaded apps
try:
    cv2.setNumThreads(1)
except Exception:
    pass


class CameraSimulator:
    def __init__(self, room_name: str):
        self.room_name = (room_name or "").strip()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.frame = None
        self.thread: Optional[threading.Thread] = None
        self.last_frame_ts: float = 0.0

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(
            target=self._generate_frames,
            name=f"CameraSim-{self.room_name}",
            daemon=True
        )
        self.thread.start()

    def stop(self):
        self._stop.set()
        t = self.thread
        if t and t.is_alive():
            t.join(timeout=1.5)
        self.thread = None

    def _generate_frames(self):
        while not self._stop.is_set():
            frame = np.random.randint(20, 80, (480, 640, 3), dtype=np.uint8)

            gradient = np.linspace(30, 100, 480).reshape(480, 1)
            frame = frame + gradient[:, :, np.newaxis].astype(np.uint8)
            frame = np.clip(frame, 0, 255)

            cv2.putText(frame, f"Room: {self.room_name}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(frame, timestamp, (10, 460),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

            bed_color = (80, 120, 180)
            cv2.rectangle(frame, (200, 200), (440, 380), bed_color, -1)
            cv2.putText(frame, "BED", (280, 300),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

            with self._lock:
                self.frame = frame
                self.last_frame_ts = time.time()

            time.sleep(0.033)

    def has_frame(self) -> bool:
        with self._lock:
            return self.frame is not None

    def get_frame(self):
        with self._lock:
            if self.frame is None:
                return None
            frame = self.frame.copy()

        ret, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buffer.tobytes() if ret else None


class RealCameraStream:
    def __init__(self, rtsp_url: str, room_name: str):
        self.rtsp_url = rtsp_url
        self.room_name = (room_name or "").strip()

        self._stop = threading.Event()
        self._frame_lock = threading.Lock()
        self._cap_lock = threading.Lock()

        self.cap: Optional[cv2.VideoCapture] = None
        self.frame = None
        self.thread: Optional[threading.Thread] = None

        self.reconnect_delay = 3
        self.last_frame_ts: float = 0.0

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(
            target=self._capture_frames,
            name=f"RTSP-{self.room_name}",
            daemon=True
        )
        self.thread.start()

    def stop(self):
        self._stop.set()

        # release capture first so read() unblocks
        with self._cap_lock:
            if self.cap:
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None

        t = self.thread
        if t and t.is_alive():
            t.join(timeout=2.5)
        self.thread = None

    def _capture_frames(self):
        while not self._stop.is_set():
            cap: Optional[cv2.VideoCapture] = None
            try:
                print(f"[Camera] Connecting to {self.room_name}: {self.rtsp_url}")

                cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
                try:
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass

                if not cap.isOpened():
                    print(f"[Camera] Failed to open RTSP stream for {self.room_name}")
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = None
                    time.sleep(self.reconnect_delay)
                    continue

                with self._cap_lock:
                    self.cap = cap

                print(f"[Camera] Connected to {self.room_name}")

                while not self._stop.is_set():
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        print(f"[Camera] Lost connection to {self.room_name}")
                        break

                    h, w = frame.shape[:2]
                    if w > 1280:
                        scale = 1280 / w
                        frame = cv2.resize(frame, (1280, int(h * scale)))

                    overlay = frame.copy()
                    cv2.rectangle(overlay, (5, 5), (320, 80), (0, 0, 0), -1)
                    cv2.addWeighted(overlay, 0.5, frame, 0.5, 0, frame)

                    cv2.putText(frame, f"Room: {self.room_name}", (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    hh, ww = frame.shape[:2]
                    cv2.rectangle(frame, (5, hh - 35), (260, hh - 5), (0, 0, 0), -1)
                    cv2.putText(frame, timestamp, (10, hh - 15),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

                    pulse = int(abs(np.sin(time.time() * 3) * 255))
                    cv2.circle(frame, (ww - 40, 30), 12, (0, pulse, 0), -1)
                    cv2.putText(frame, "LIVE", (ww - 85, 35),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

                    with self._frame_lock:
                        self.frame = frame
                        self.last_frame_ts = time.time()

                    time.sleep(0.005)

            except Exception as e:
                print(f"[Camera] Error in {self.room_name}: {str(e)}")

            finally:
                if cap:
                    try:
                        cap.release()
                    except Exception:
                        pass
                with self._cap_lock:
                    if self.cap is cap:
                        self.cap = None

                if not self._stop.is_set():
                    time.sleep(self.reconnect_delay)

    def has_frame(self) -> bool:
        with self._frame_lock:
            return self.frame is not None

    def get_frame(self):
        with self._frame_lock:
            if self.frame is None:
                return None
            frame = self.frame.copy()

        ret, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buffer.tobytes() if ret else None


StreamType = Union[CameraSimulator, RealCameraStream]


class CameraManager:
    def __init__(self, use_simulation=True):
        self.cameras: Dict[str, StreamType] = {}
        self.use_simulation = use_simulation
        self._lock = threading.Lock()
        atexit.register(self.shutdown)

    def _key(self, room_name: str) -> str:
        return (room_name or "").strip()

    def add_camera(self, room_name: str, rtsp_url: str = None):
        key = self._key(room_name)
        with self._lock:
            if key in self.cameras:
                return

            if rtsp_url and rtsp_url.startswith("demo://"):
                cam: StreamType = CameraSimulator(key)
            elif self.use_simulation:
                cam = CameraSimulator(key)
            else:
                if not rtsp_url:
                    raise ValueError(f"RTSP URL required for {key}")
                cam = RealCameraStream(rtsp_url, key)

            cam.start()
            self.cameras[key] = cam
            print(f"[Camera] Added camera for {key}")

    def remove_camera(self, room_name: str):
        key = self._key(room_name)
        with self._lock:
            cam = self.cameras.pop(key, None)
        if cam:
            cam.stop()

    def get_frame(self, room_name: str):
        key = self._key(room_name)
        with self._lock:
            cam = self.cameras.get(key)
        return cam.get_frame() if cam else None

    def has_frame(self, room_name: str) -> bool:
        key = self._key(room_name)
        with self._lock:
            cam = self.cameras.get(key)
        return cam.has_frame() if cam else False

    def get_all_rooms(self):
        with self._lock:
            return list(self.cameras.keys())

    def shutdown(self):
        with self._lock:
            cams = list(self.cameras.values())
            self.cameras.clear()
        for cam in cams:
            try:
                cam.stop()
            except Exception:
                pass
