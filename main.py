import asyncio
import logging
import os
from typing import Dict, Any, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fs5000 import FS5000, MockFS5000, get_port

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

app = FastAPI(title="FS5000 Radiation Detector API", version="1.0")

# (Opsional) izinkan akses dari frontend lain
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# WebSocket connection registry
active_connections: list[WebSocket] = []

# Global device & last sample cache
fs5000_device: Optional[FS5000] = None
latest_data: Optional[Dict[str, Any]] = None


def parse_stream_record(datum: str) -> Dict[str, Any]:
    """
    Convert "2025-08-13T14:22:10;DR:0.15uSv/h;D:1.63uSv;..." -> dict JSON
    """
    parts = [p for p in datum.split(";") if p]  # buang empty segmen
    if not parts:
        return {}
    data: Dict[str, Any] = {"timestamp": parts[0]}
    for p in parts[1:]:
        if ":" in p:
            k, v = p.split(":", 1)
            data[k.strip()] = v.strip()
    return data


@app.on_event("startup")
def startup_connect():
    global fs5000_device
    use_mock = os.getenv("FS5000_USE_MOCK", "").lower() in ("1", "true", "yes")
    if use_mock:
        fs5000_device = MockFS5000("/dev/null")
        logging.warning("Using MockFS5000 (FS5000_USE_MOCK=1)")
    else:
        port = get_port()  # autodetect by VID/PID
        fs5000_device = FS5000(port)
        logging.info(f"Connected to FS5000 at {port}")


async def broadcast_data():
    """
    Background task:
    - membaca data kontinu dari FS5000.yield_data()
    - parsing -> JSON
    - update latest_data
    - kirim ke semua WebSocket client
    """
    global latest_data
    assert fs5000_device is not None, "Device not initialized"

    while True:
        try:
            for datum in fs5000_device.yield_data():
                data_dict = parse_stream_record(datum)
                if not data_dict:
                    continue

                latest_data = data_dict  # cache untuk endpoint HTTP

                # kirim ke semua klien
                stale: list[WebSocket] = []
                for ws in active_connections:
                    try:
                        await ws.send_json(data_dict)
                    except WebSocketDisconnect:
                        stale.append(ws)
                    except Exception as e:
                        logging.error(f"WebSocket send error: {e}")
                        stale.append(ws)

                for ws in stale:
                    if ws in active_connections:
                        active_connections.remove(ws)

                # Jeda kecil agar tidak terlalu cepat (sesuaikan jika perlu)
                await asyncio.sleep(0.2)
        except Exception as e:
            logging.error(f"Error in broadcast loop: {e}")
            await asyncio.sleep(1)  # retry loop bila error


@app.on_event("startup")
async def start_stream_task():
    # mulai task background streaming
    asyncio.create_task(broadcast_data())


@app.get("/health")
def health():
    ok = fs5000_device is not None
    return {"status": "ok" if ok else "not_ready"}


@app.get("/dose")
def get_current_dose():
    """
    Ambil sampel terakhir (JSON) dari cache streaming.
    Tidak memanggil get_dose() agar tidak bentrok dengan mode read kontinu.
    """
    if latest_data is None:
        return JSONResponse(status_code=503, content={"error": "No data yet"})
    return latest_data


@app.websocket("/ws")
async def ws_stream(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    logging.info("WebSocket client connected")
    try:
        # Kita tidak menerima pesan dari client; hanya push data.
        while True:
            await asyncio.sleep(30)  # keep-alive no-op
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)
        logging.info("WebSocket client disconnected")
