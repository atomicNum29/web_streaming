from http.server import HTTPServer, BaseHTTPRequestHandler
import os
import struct
import threading
import time
from dataclasses import dataclass
from socketserver import ThreadingMixIn
from typing import Dict, Iterator, Optional
from urllib.parse import urlparse

import cv2
from picamera2 import Picamera2

import serial
import serial.tools.list_ports


@dataclass
class AppConfig:
    capture_interval: float = 0.5
    jpeg_quality: int = 80
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    serial_out_port: Optional[str] = None
    serial_baud: int = 115200
    serial_timeout: float = 1.0


def load_config() -> AppConfig:
    """Load runtime configuration (currently hard-coded)."""

    def optional_env(name: str) -> Optional[str]:
        value = os.getenv(name)
        return value if value else None

    return AppConfig(
        capture_interval=0.03,
        jpeg_quality=80,
        host="0.0.0.0",
        port=8000,
        debug=False,
        serial_out_port=optional_env("SERIAL_OUT_PORT"),
        serial_baud=int(os.getenv("SERIAL_BAUD", "115200")),
        serial_timeout=float(os.getenv("SERIAL_TIMEOUT", "1.0")),
    )


def find_teensy_port() -> Optional[str]:
    """
    Try to auto-detect a Teensy/USB-UART device.
    Looks for "Teensy", "usbmodem", "ttyACM" or common USB serial identifiers.
    """
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").lower()
        name = (p.device or "").lower()
        if "teensy" in desc or "teensy" in name:
            return p.device
    # fallback heuristics
    for p in serial.tools.list_ports.comports():
        name = (p.device or "").lower()
        if (
            "usbmodem" in name
            or "ttyacm" in name
            or "usbserial" in name
            or "ttyusb" in name
        ):
            return p.device
    return None


SERIAL_SEND_LOCK = threading.Lock()


def send_command(
    v_value: float,
    w_value: float,
    port: Optional[str] = None,
    baud: int = 115200,
    timeout: float = 1.0,
) -> str:
    """
    Open serial port to Teensy and send a command value (as a line).
    Returns the first line of response (empty string if none).
    """
    with SERIAL_SEND_LOCK:
        if port is None:
            port = find_teensy_port()
            if port is None:
                raise RuntimeError("Teensy port not found. Provide port explicitly.")
        with serial.Serial(port, baud, timeout=timeout) as ser:
            v = float(v_value)
            w = float(w_value)
            payload = b"\xaa\x55" + struct.pack("<ff", v, w) + b"\x55\xaa"
            ser.write(payload)
            # give device a short moment to respond
            time.sleep(0.05)
            try:
                resp = ser.readline().decode("utf-8", errors="replace").strip()
            except Exception:
                resp = ""
            return resp


def _direction_to_vw(direction: Optional[str]) -> Optional[tuple[float, float]]:
    if not direction:
        return None
    direction = direction.lower()
    if direction == "forward":
        return 0.2, 0.0
    if direction == "backward":
        return -0.2, 0.0
    if direction == "left":
        return 0.0, 1.0
    if direction == "right":
        return 0.0, -1.0
    return None


def _resolve_command(
    cmd: Optional[str], dir_: Optional[str]
) -> Optional[tuple[float, float]]:
    if not cmd:
        return None
    cmd = cmd.lower()
    if cmd == "stop":
        return 0.0, 0.0
    if cmd == "go":
        return _direction_to_vw(dir_)
    return None


def dispatch_command(v: float, w: float, config: AppConfig, source: str) -> None:
    try:
        resp = send_command(
            v,
            w,
            port=config.serial_out_port,
            baud=config.serial_baud,
            timeout=config.serial_timeout,
        )
        print(f"Sent command from {source}: v={v}, w={w}, resp='{resp}'")
    except Exception as exc:
        print(f"Failed to send command from {source}: {exc}")


class CameraInferenceService:
    """Continuously grab frames from Picamera2 and publish JPEG streams."""

    def __init__(self, config: AppConfig, camera_num: int) -> None:
        self.config = config
        self.picam2 = Picamera2(camera_num=camera_num)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._latest_original: Optional[bytes] = None
        self._frame_seq: int = 0

    def start(self) -> None:
        camera_config = self.picam2.create_video_configuration(
            main={"size": (1640, 1232), "format": "RGB888"},
            buffer_count=32,
            sensor={"bit_depth": 8},
            controls={"FrameDurationLimits": (10000, 33333)},
        )
        self.picam2.configure(camera_config)
        self.picam2.start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        try:
            self.picam2.stop()
        except Exception:
            pass
        try:
            self.picam2.close()
        except Exception:
            pass

    def frame_generator(self) -> Iterator[bytes]:
        boundary = b"--frame"
        last_seq = -1
        while not self._stop.is_set():
            with self._condition:
                while last_seq == self._frame_seq and not self._stop.is_set():
                    self._condition.wait(timeout=1.0)
                if self._stop.is_set():
                    break
                frame = self._latest_original

                seq = self._frame_seq
            if frame is None:
                continue
            last_seq = seq
            yield (boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")

    def _loop(self) -> None:
        """Capture frames, store original/detection images, and publish latest paths."""
        jpeg_quality = int(max(10, min(95, self.config.jpeg_quality)))
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]
        while not self._stop.is_set():
            try:
                bgr = self.picam2.capture_array()
            except Exception as exc:
                print(f"Camera capture failed: {exc}")
                time.sleep(0.2)
                continue

            if bgr is None:
                time.sleep(0.05)
                continue

            ok_orig, orig_buf = cv2.imencode(".jpg", bgr, encode_params)
            if not ok_orig:
                print("JPEG encoding failed, skipping frame.")
                continue

            with self._condition:
                self._latest_original = orig_buf.tobytes()
                self._frame_seq += 1
                self._condition.notify_all()

            if self.config.capture_interval > 0:
                time.sleep(self.config.capture_interval)


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
    <title>Live Camera Stream</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; background: #f3f3f3; }
    .wrapper { background: #fff; padding: 16px; border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.1); }
    .images { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-top: 12px; }
    img { width: 100%; border: 1px solid #ddd; border-radius: 6px; background: #fafafa; min-height: 160px; }
    h1 { margin-bottom: 4px; }
    p { margin: 0; }
  </style>
</head>
<body>
  <div class="wrapper">
    <h1>Camera Monitor</h1>
    <div class="images">
      <div>
        <img id="img-original0" src="/stream0" alt="Original stream">
      </div>
      <div>
        <img id="img-original1" src="/stream1" alt="Original stream">
      </div>
    </div>
  </div>
<script>
    const keys = {};
    
    window.addEventListener('keydown', (e) => {
        const k = e.key.toLowerCase();
        if (keys[k]) return;          // 이미 눌린 키면 무시
        keys[k] = true;
        // console.log(`key down: ${k}`);
        buttonPress(getCommand(k));
    });
    
    window.addEventListener('keyup', (e) => {
        const k = e.key.toLowerCase();
        keys[k] = false;
        // console.log(`key up: ${k}`);
        buttonRelease(getCommand(k));
    });
    
    function getCommand(key) {
        if (key === 'arrowup') return 'forward';
        if (key === 'arrowdown') return 'backward';
        if (key === 'arrowleft') return 'left';
        if (key === 'arrowright') return 'right';
        return null;
    }
    
    function buttonPress(direction) {
        fetch(`/control?cmd=go&dir=${direction}`);
        console.log(`key down: ${direction}`);
    }
    
    function buttonRelease(direction) {
        fetch(`/control?cmd=stop&dir=${direction}`);
        console.log(`key up: ${direction}`);
    }
</script>
<div style="margin-top: 20px; text-align: center;">
    <div style="margin-bottom: 10px;">
        <button onmousedown="buttonPress('forward')" onmouseup="buttonRelease('forward')" style="padding: 10px 20px; font-size: 20px;">↑</button>
    </div>
    <div style="margin-bottom: 10px;">
        <button onmousedown="buttonPress('left')" onmouseup="buttonRelease('left')" style="padding: 10px 20px; margin-right: 10px; font-size: 20px;">←</button>
        <button onmousedown="buttonPress('backward')" onmouseup="buttonRelease('backward')" style="padding: 10px 20px; margin-right: 10px; font-size: 20px;">↓</button>
        <button onmousedown="buttonPress('right')" onmouseup="buttonRelease('right')" style="padding: 10px 20px; font-size: 20px;">→</button>
    </div>
</div>
</body>
</html>
"""


# 1. 멀티 스레딩이 가능한 서버 클래스 정의
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""

    daemon_threads = True
    services: Dict[int, "CameraInferenceService"]
    html_template: str
    config: AppConfig


class SimpleHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "ThreadedHTTPServer"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html()
            return

        if path == "/control":
            qs = self._parse_qs(parsed.query)
            cmd = qs.get("cmd", [None])[0]
            dir_ = qs.get("dir", [None])[0]

            if cmd is None:
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if cmd != "stop" and dir_ is None:
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            resolved = _resolve_command(cmd, dir_)
            if resolved is None:
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            v, w = resolved
            dispatch_command(v, w, self.server.config, source=f"http:{cmd}:{dir_}")
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        stream_index = self._parse_stream_index(path)
        if stream_index is not None:
            service = self._get_service(stream_index)
            if service is None:
                self.send_response(404)
                self.end_headers()
                return
            self._stream_mjpeg(service)
            return

        self.send_response(404)
        self.end_headers()

    def _send_html(self) -> None:
        body = self.server.html_template.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_stream_index(self, path: str) -> Optional[int]:
        if not path.startswith("/stream"):
            return None
        suffix = path[len("/stream") :]
        if not suffix.isdigit():
            return None
        return int(suffix)

    def _parse_qs(self, path: str) -> Dict[str, list[str]]:
        qs: Dict[str, list[str]] = {}
        for pair in path.split("&"):
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            if key not in qs:
                qs[key] = []
            qs[key].append(value)
        return qs

    def _get_service(self, index: int) -> Optional["CameraInferenceService"]:
        return self.server.services.get(index)

    def _stream_mjpeg(self, service: "CameraInferenceService") -> None:
        try:
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            self.end_headers()
            for chunk in service.frame_generator():
                try:
                    self.wfile.write(chunk)
                except BrokenPipeError:
                    break
        except Exception:
            pass


def main() -> None:
    config = load_config()
    services = {
        0: CameraInferenceService(config, camera_num=0),
        1: CameraInferenceService(config, camera_num=1),
    }
    for service in services.values():
        service.start()

    httpd = ThreadedHTTPServer((config.host, config.port), SimpleHandler)
    httpd.services = services
    httpd.html_template = HTML_TEMPLATE
    httpd.config = config
    print(f"Serving live stream at http://{config.host}:{config.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        for service in services.values():
            service.stop()


if __name__ == "__main__":
    main()
