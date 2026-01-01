import cv2
import numpy as np
from datetime import datetime
import threading
from typing import Dict
import time

class CameraSimulator:
    """Simulates a camera feed for demo purposes"""
    
    def __init__(self, room_name: str):
        self.room_name = room_name
        self.frame = None
        self.is_running = False
        self.thread = None
        
    def start(self):
        if not self.is_running:
            self.is_running = True
            self.thread = threading.Thread(target=self._generate_frames, daemon=True)
            self.thread.start()
    
    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join()
    
    def _generate_frames(self):
        while self.is_running:
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
            
            pulse_value = int(50 * (1 + np.sin(time.time() * 2)))
            cv2.circle(frame, (580, 50), 20, (0, 255, 0), -1)
            cv2.putText(frame, "LIVE", (550, 90), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
            
            self.frame = frame
            time.sleep(0.033)
    
    def get_frame(self):
        if self.frame is None:
            return None
        
        ret, buffer = cv2.imencode('.jpg', self.frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ret:
            return buffer.tobytes()
        return None


class RealCameraStream:
    """Real RTSP camera stream handler"""
    
    def __init__(self, rtsp_url: str, room_name: str):
        self.rtsp_url = rtsp_url
        self.room_name = room_name
        self.cap = None
        self.frame = None
        self.is_running = False
        self.thread = None
        self.reconnect_delay = 5
        
    def start(self):
        if not self.is_running:
            self.is_running = True
            self.thread = threading.Thread(target=self._capture_frames, daemon=True)
            self.thread.start()
    
    def stop(self):
        self.is_running = False
        if self.cap:
            self.cap.release()
        if self.thread:
            self.thread.join()
    
    def _capture_frames(self):
        while self.is_running:
            try:
                print(f"[Camera] Connecting to {self.room_name}: {self.rtsp_url}")
                
                self.cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self.cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 30000)
                self.cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 30000)
                self.cap.set(cv2.CAP_PROP_FPS, 25)
                
                if not self.cap.isOpened():
                    print(f"[Camera] Failed to open RTSP stream for {self.room_name}")
                    time.sleep(self.reconnect_delay)
                    continue
                
                print(f"[Camera] Connected to {self.room_name}")
                
                while self.is_running:
                    ret, frame = self.cap.read()
                    if not ret:
                        print(f"[Camera] Lost connection to {self.room_name}")
                        break
                    
                    height, width = frame.shape[:2]
                    if width > 1280:
                        scale = 1280 / width
                        frame = cv2.resize(frame, (1280, int(height * scale)))
                        height, width = frame.shape[:2]
                    
                    # Add overlay
                    overlay = frame.copy()
                    cv2.rectangle(overlay, (5, 5), (300, 80), (0, 0, 0), -1)
                    cv2.addWeighted(overlay, 0.5, frame, 0.5, 0, frame)
                    
                    cv2.putText(frame, f"Room: {self.room_name}", (10, 30), 
                               cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cv2.rectangle(frame, (5, height - 35), (250, height - 5), (0, 0, 0), -1)
                    cv2.putText(frame, timestamp, (10, height - 15), 
                               cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                    
                    pulse = int(abs(np.sin(time.time() * 3) * 255))
                    cv2.circle(frame, (width - 40, 30), 12, (0, pulse, 0), -1)
                    cv2.putText(frame, "LIVE", (width - 85, 35), 
                               cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                    
                    self.frame = frame
                    
            except Exception as e:
                print(f"[Camera] Error in {self.room_name}: {str(e)}")
            finally:
                if self.cap:
                    self.cap.release()
                if self.is_running:
                    time.sleep(self.reconnect_delay)
    
    def get_frame(self):
        if self.frame is None:
            return None
        
        ret, buffer = cv2.imencode('.jpg', self.frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ret:
            return buffer.tobytes()
        return None


class CameraManager:
    """Manages multiple camera streams"""
    
    def __init__(self, use_simulation=True):
        self.cameras: Dict[str, CameraSimulator | RealCameraStream] = {}
        self.use_simulation = use_simulation
    
    def add_camera(self, room_name: str, rtsp_url: str = None):
        if room_name in self.cameras:
            return
        
        if rtsp_url and rtsp_url.startswith('demo://'):
            camera = CameraSimulator(room_name)
        elif self.use_simulation:
            camera = CameraSimulator(room_name)
        else:
            if not rtsp_url:
                raise ValueError(f"RTSP URL required for {room_name}")
            camera = RealCameraStream(rtsp_url, room_name)
        
        camera.start()
        self.cameras[room_name] = camera
        print(f"[Camera] Added camera for {room_name}")
    
    def remove_camera(self, room_name: str):
        if room_name in self.cameras:
            self.cameras[room_name].stop()
            del self.cameras[room_name]
    
    def get_frame(self, room_name: str):
        if room_name in self.cameras:
            return self.cameras[room_name].get_frame()
        return None
    
    def get_all_rooms(self):
        return list(self.cameras.keys())
    
    def shutdown(self):
        for camera in self.cameras.values():
            camera.stop()
        self.cameras.clear()
