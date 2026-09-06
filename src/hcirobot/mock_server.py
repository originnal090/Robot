from __future__ import annotations

import json
import socket
import threading
import time


class MockRobotServer:
    """Small JSONL receiver used to verify the TonyPi-compatible TCP path."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5075, watchdog: float = 0.6) -> None:
        self.host = host
        self.port = port
        self.watchdog = watchdog
        self.commands: list[dict] = []
        self.bound_port = 0
        self._stop = threading.Event()
        self._listener: socket.socket | None = None

    def serve_forever(self, ready: threading.Event | None = None) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(1)
        listener.settimeout(0.2)
        self._listener = listener
        self.bound_port = listener.getsockname()[1]
        if ready is not None:
            ready.set()
        try:
            while not self._stop.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                self._handle(connection)
        finally:
            listener.close()

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(0.1)
        buffer = b""
        last_valid = time.monotonic()
        with connection:
            while not self._stop.is_set():
                try:
                    chunk = connection.recv(4096)
                except TimeoutError:
                    if time.monotonic() - last_valid >= self.watchdog:
                        self.commands.append({"v": 0.0, "steer": 0.0, "grab": False, "watchdog": True})
                        return
                    continue
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > 65536:
                    return
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.startswith(b"CMD:"):
                        name = line[4:].decode("utf-8", errors="replace").strip()
                        self.commands.append({"cmd": name})
                        last_valid = time.monotonic()
                        continue
                    try:
                        payload = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if all(key in payload for key in ("v", "steer", "grab", "t")):
                        self.commands.append(payload)
                        last_valid = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass


def main() -> int:
    server = MockRobotServer()
    print(f"mock robot listening on {server.host}:{server.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
