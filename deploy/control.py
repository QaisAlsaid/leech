from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ControlSidecar:
    def __init__(self, supervisor, port: int, token: str):
        self.supervisor = supervisor
        self.port = port
        self.token = (token or "").strip()
        outer = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                # a client that hung up mid-request (the channel cog's
                # httpx timeout, a curl that got bored) used to print a
                # full BrokenPipeError traceback per occurrence. it is
                # not an error on our side - swallow the disconnects and
                # keep serving.
                exc = sys.exc_info()[1]
                if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                                    ConnectionAbortedError)):
                    return
                super().handle_error(request, client_address)

        class Handler(BaseHTTPRequestHandler):
            server_version = "leech-control/1"

            def log_message(self, format, *args):
                pass

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, indent=2).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError, socket.timeout):
                    # the caller stopped listening (curl --max-time,
                    # httpx read timeout). the mutation itself still ran
                    # on the supervisor side - losing the response is the
                    # correct outcome, not a traceback.
                    pass

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    data = json.loads(self.rfile.read(length))
                    return data if isinstance(data, dict) else {}
                except (ValueError, OSError):
                    return {}

            def _authorized(self) -> bool:
                if not outer.token:
                    return False
                supplied = (self.headers.get("x-leech-token") or "").strip()
                return supplied == outer.token

            def do_GET(self):
                if self.path.rstrip("/") in ("/", ""):
                    self._send(200, outer.supervisor.status())
                else:
                    self._send(404, {"ok": False, "error": "no such route"})

            def do_POST(self):
                route = self.path.rstrip("/")
                mutations = {
                    "/switch", "/pull", "/revert",
                    "/autopull", "/shutdown",
                }
                if route not in mutations:
                    self._send(404, {"ok": False, "error": "no such route"})
                    return
                if not outer.token:
                    self._send(503, {"ok": False,
                                     "error": "SUPERVISOR_TOKEN is not set - "
                                              "mutations are refused"})
                    return
                if not self._authorized():
                    self._send(401, {"ok": False, "error": "bad token"})
                    return
                body = self._body()
                if route == "/switch":
                    result = outer.supervisor.request(
                        "switch", channel=str(body.get("channel") or ""))
                elif route == "/pull":
                    result = outer.supervisor.request(
                        "pull",
                        force=bool(body.get("force")),
                        commit=str(body.get("commit") or "").strip(),
                    )
                elif route == "/revert":
                    result = outer.supervisor.request(
                        "revert",
                        commit=str(body.get("commit") or "").strip(),
                    )
                elif route == "/autopull":
                    result = outer.supervisor.request(
                        "autopull", on=bool(body.get("on")))
                else:
                    result = outer.supervisor.request("shutdown")
                self._send(200, result)

        self.handler = Handler
        self.server_class = Server

    def start(self) -> None:
        server = self.server_class(("0.0.0.0", self.port), self.handler)
        thread = threading.Thread(target=server.serve_forever,
                                  name="control-sidecar", daemon=True)
        thread.start()
        gating = "token-gated" if self.token else (
            "disabled for mutations - set SUPERVISOR_TOKEN")
        print(f"[control] listening on 0.0.0.0:{self.port} ({gating})",
              flush=True)
