from __future__ import annotations

import asyncio
import os
import struct
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional

import cv2
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from picamera2 import Picamera2

import serial
import serial.tools.list_ports


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class AppConfig:
    capture_interval: float = 0.03
    jpeg_quality: int = 80
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    serial_out_port: Optional[str] = None
    serial_baud: int = 115200
    serial_timeout: float = 0.0
    camera_ids: tuple[int, ...] = (0, 1)


def load_config() -> AppConfig:
    """Load runtime configuration from environment variables."""

    def optional_env(name: str) -> Optional[str]:
        value = os.getenv(name)
        return value if value else None

    def parse_camera_ids(value: str) -> tuple[int, ...]:
        ids: list[int] = []
        for chunk in value.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                ids.append(int(chunk))
            except ValueError:
                continue
        return tuple(ids) if ids else (0,)

    camera_ids = parse_camera_ids(os.getenv("CAMERA_IDS", "0,1"))

    return AppConfig(
        capture_interval=float(os.getenv("CAPTURE_INTERVAL", "0.03")),
        jpeg_quality=int(os.getenv("JPEG_QUALITY", "80")),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        debug=os.getenv("DEBUG", "false").lower() == "true",
        serial_out_port=optional_env("SERIAL_OUT_PORT"),
        serial_baud=int(os.getenv("SERIAL_BAUD", "115200")),
        serial_timeout=float(os.getenv("SERIAL_TIMEOUT", "0.0")),
        camera_ids=camera_ids,
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
    timeout: float = 0.0,
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
        return 0.0, 4.0
    if direction == "right":
        return 0.0, -4.0
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


def _dispatch_command_sync(v: float, w: float, config: AppConfig, source: str) -> None:
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


async def dispatch_command_async(
    v: float, w: float, config: AppConfig, source: str
) -> None:
    await asyncio.to_thread(_dispatch_command_sync, v, w, config, source)


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
            yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"

    def _loop(self) -> None:
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    services: Dict[int, CameraInferenceService] = {}
    for camera_id in config.camera_ids:
        try:
            service = CameraInferenceService(config, camera_num=camera_id)
            service.start()
            services[camera_id] = service
        except Exception as exc:
            print(f"Failed to start camera {camera_id}: {exc}")
    app.state.config = config
    app.state.services = services
    yield
    for service in services.values():
        service.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    services: Dict[int, CameraInferenceService] = request.app.state.services
    camera_ids = sorted(services.keys())
    return templates.TemplateResponse(
        "index.html", {"request": request, "camera_ids": camera_ids}
    )


@app.get("/stream/{camera_id}")
async def stream_camera(camera_id: int, request: Request):
    services: Dict[int, CameraInferenceService] = request.app.state.services
    service = services.get(camera_id)
    if service is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return StreamingResponse(
        service.frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/control")
async def control(
    request: Request,
    cmd: str = Query(...),
    dir_: Optional[str] = Query(None, alias="dir"),
):
    resolved = _resolve_command(cmd, dir_)
    if resolved is None:
        raise HTTPException(status_code=400, detail="Invalid command")
    v, w = resolved
    config: AppConfig = request.app.state.config
    await dispatch_command_async(v, w, config, source=f"http:{cmd}:{dir_}")
    return JSONResponse({"ok": True})


@app.websocket("/ws/control")
async def control_ws(websocket: WebSocket):
    await websocket.accept()
    config: AppConfig = websocket.app.state.config
    try:
        while True:
            try:
                payload = await websocket.receive_json()
            except ValueError:
                await websocket.send_json(
                    {"ok": False, "error": "Invalid JSON payload"}
                )
                continue
            cmd = payload.get("cmd")
            dir_ = payload.get("dir")
            v = payload.get("v")
            w = payload.get("w")
            if v is not None and w is not None:
                await dispatch_command_async(float(v), float(w), config, "ws:vw")
                await websocket.send_json({"ok": True})
                continue

            resolved = _resolve_command(cmd, dir_)
            if resolved is None:
                await websocket.send_json({"ok": False, "error": "Invalid command"})
                continue
            rv, rw = resolved
            await dispatch_command_async(rv, rw, config, f"ws:{cmd}:{dir_}")
            await websocket.send_json({"ok": True})
    except WebSocketDisconnect:
        return


@app.get("/health")
async def health(request: Request):
    services: Dict[int, CameraInferenceService] = request.app.state.services
    return {"cameras": sorted(services.keys())}


if __name__ == "__main__":
    import uvicorn

    config = load_config()
    uvicorn.run(
        "web_streaming:app",
        host=config.host,
        port=config.port,
        reload=config.debug,
    )
