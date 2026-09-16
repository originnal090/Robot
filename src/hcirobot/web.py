"""LAN web console. Start with hcirobot-web; one process owns the robot."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import ipaddress
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .robot import parse_endpoint
from .service import ControlConflict, SessionService

STATIC = Path(__file__).with_name("web_static")
Axis = Annotated[float, Field(ge=-1, le=1)]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Owner(Payload):
    client_id: str = Field(min_length=1, max_length=64)
    lease: str = Field(min_length=1, max_length=64)


class SessionRequest(Owner):
    session_id: int = Field(ge=0)
    arm_mode: Literal["direct", "startup"] = "direct"


class StartRequest(Owner):
    source: str | None = Field(default=None, min_length=1, max_length=2048)
    backend: Literal["tcp", "recording"] | None = None
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    mirror: str = Field(default="", max_length=255)
    control_only: bool = False
    local_gamepad: bool = False
    obstacle: bool | None = None
    startup_forward: bool = False
    approach_mode: Literal["normal", "normal_then_slow", "slow_realtime"] | None = None
    detector_backend: Literal["lab", "ascend", "svm"] | None = None
    detector_model_path: str | None = Field(default=None, max_length=2048)


class TuneRequest(SessionRequest):
    detection: dict = Field(default_factory=dict)
    controller: dict = Field(default_factory=dict)


class CaptureRequest(SessionRequest):
    active: bool


class ClaimRequest(Payload):
    client_id: str = Field(min_length=1, max_length=64)


class LoginRequest(Payload):
    token: str = Field(min_length=1, max_length=1024)


class Heartbeat(Payload):
    type: Literal["heartbeat"]
    lease: str = Field(min_length=1, max_length=64)


class Manual(Heartbeat):
    type: Literal["manual"]
    session_id: int = Field(ge=1)
    seq: int = Field(ge=0)
    velocity: Axis
    steer: Axis
    lateral: Axis = 0.0


def same_origin(connection) -> bool:
    origin = connection.headers.get("origin")
    if origin is None:
        return True  # non-browser API clients still need authentication
    parsed = urlsplit(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc == connection.headers.get("host")


def requires_auth(path: str) -> bool:
    """Keep control/state private while allowing the read-only Unity video feed."""
    return path.startswith("/api/") or path == "/stream.mjpg"


def create_app(
    config: Path = Path("config.toml"),
    *,
    token: str | None = None,
    gamepad: bool = False,
    service: SessionService | None = None,
) -> FastAPI:
    runtime = service or SessionService(config, gamepad=gamepad)

    @asynccontextmanager
    async def lifespan(_app):
        runtime.open()
        try:
            yield
        finally:
            await asyncio.to_thread(runtime.close)

    app = FastAPI(
        title="HCIRobot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.service = runtime

    def authorized(connection) -> bool:
        if not token:
            return True
        candidate = connection.cookies.get("hcirobot_access", "")
        header = connection.headers.get("authorization", "")
        if header.startswith("Bearer "):
            candidate = header[7:]
        return hmac.compare_digest(candidate.encode(), token.encode())

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD") and not same_origin(request):
            return JSONResponse({"detail": "拒绝跨站控制请求"}, status_code=403)
        protected = requires_auth(request.url.path)
        if protected and request.url.path != "/api/login" and not authorized(request):
            return JSONResponse({"detail": "请输入访问令牌"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob:; connect-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(ControlConflict)
    async def conflict(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/api/login")
    def login(data: LoginRequest, request: Request):
        if token and not hmac.compare_digest(data.token.encode(), token.encode()):
            return JSONResponse({"detail": "访问令牌不正确"}, status_code=401)
        response = JSONResponse({"ok": True})
        if token:
            response.set_cookie(
                "hcirobot_access",
                token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
            )
        return response

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/defaults")
    def defaults():
        return runtime.defaults()

    @app.get("/api/state")
    def state():
        return runtime.snapshot()

    @app.post("/api/control/claim")
    def claim(data: ClaimRequest):
        return {"lease": runtime.claim(data.client_id)}

    @app.post("/api/control/release")
    def release(data: Owner):
        runtime.release(data.client_id, data.lease)
        return {"ok": True}

    @app.post("/api/session/start")
    def start(data: StartRequest):
        options = runtime.defaults() | data.model_dump(exclude_none=True)
        options = {
            key: options[key]
            for key in StartRequest.model_fields
            if key not in ("client_id", "lease")
        }
        if not options["host"].strip() or not options["source"].strip():
            raise ValueError("视频源和机器人地址不能为空")
        options["mirror"] = parse_endpoint(options["mirror"]) if options["mirror"] else None
        runtime.start_session(data.client_id, data.lease, options)
        return {"ok": True}

    # Any authenticated observer can stop the robot, including a local-pad owner.
    @app.post("/api/session/stop")
    def stop():
        runtime.stop()
        return {"ok": True}

    @app.post("/api/session/estop")
    def estop():
        runtime.stop(estop=True)
        return {"ok": True}

    @app.post("/api/session/{operation}")
    def command(operation: Literal["arm", "disarm", "reset"], data: SessionRequest):
        runtime.command(data.client_id, data.lease, data.session_id, operation, data.arm_mode)
        return {"ok": True}

    @app.post("/api/action/{name}")
    def action(name: str, data: SessionRequest):
        runtime.action(data.client_id, data.lease, data.session_id, name)
        return {"ok": True}

    @app.patch("/api/tuning")
    def tune(data: TuneRequest):
        return runtime.tune(
            data.client_id, data.lease, data.session_id, data.detection, data.controller
        )

    @app.post("/api/capture")
    def capture(data: CaptureRequest):
        runtime.capture(data.client_id, data.lease, data.session_id, data.active)
        return {"ok": True}

    def mjpeg_response(request: Request) -> StreamingResponse:
        async def frames():
            previous = -1
            while not await request.is_disconnected():
                seq, jpeg = runtime.jpeg()
                if jpeg is not None and seq != previous:
                    previous = seq
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpeg)).encode()
                        + b"\r\n\r\n"
                        + jpeg
                        + b"\r\n"
                    )
                await asyncio.sleep(0.05)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.get("/stream.mjpg")
    async def stream(request: Request):
        return mjpeg_response(request)

    @app.get("/unity-stream.mjpg")
    async def unity_stream(request: Request):
        """Unauthenticated read-only fan-out of the existing annotated JPEG cache."""
        return mjpeg_response(request)

    @app.websocket("/ws/events")
    async def events(ws: WebSocket):
        if not authorized(ws) or not same_origin(ws):
            await ws.close(code=1008)
            return
        await ws.accept()
        client = runtime.connect()
        send_lock = asyncio.Lock()

        async def send(payload):
            async with send_lock:
                await asyncio.wait_for(ws.send_json(payload), timeout=1.0)

        async def status():
            while True:
                await send(
                    {"type": "state", "client_id": client, "state": runtime.snapshot(client)}
                )
                await asyncio.sleep(0.1)

        async def receive():
            while True:
                raw = await ws.receive_text()
                if len(raw) > 4096:
                    await ws.close(code=1009)
                    return
                try:
                    # Validate raw JSON without accepting arbitrary operation names.
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise TypeError("消息必须为 JSON 对象")
                    if payload.get("type") == "manual":
                        data = Manual.model_validate(payload)
                        runtime.manual(
                            client,
                            data.lease,
                            data.session_id,
                            data.seq,
                            data.velocity,
                            data.steer,
                            data.lateral,
                        )
                    else:
                        data = Heartbeat.model_validate(payload)
                        runtime.heartbeat(client, data.lease)
                except (ValidationError, ValueError, TypeError) as exc:
                    await send({"type": "error", "message": str(exc)})

        tasks = [asyncio.create_task(status()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, TimeoutError, OSError, RuntimeError):
            pass
        finally:
            runtime.disconnect(client)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
                await ws.close()

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HCIRobot browser control console")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--gamepad", action="store_true", help="Enable server-attached gamepad")
    parser.add_argument("--ssl-certfile", type=Path)
    parser.add_argument("--ssl-keyfile", type=Path)
    args = parser.parse_args(argv)
    token = os.environ.get("HCIROBOT_WEB_TOKEN") or None
    try:
        loopback = args.host == "localhost" or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and not token:
        parser.error("局域网监听需要设置 HCIROBOT_WEB_TOKEN 环境变量")
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error("TLS 证书和私钥必须同时提供")
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    import uvicorn

    uvicorn.run(
        create_app(args.config, token=token, gamepad=args.gamepad),
        host=args.host,
        port=args.port,
        workers=1,
        ssl_certfile=str(args.ssl_certfile) if args.ssl_certfile else None,
        ssl_keyfile=str(args.ssl_keyfile) if args.ssl_keyfile else None,
        ws_max_size=4096,
        ws_max_queue=1,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
