"""A tiny HTTP server that turns requests into API Gateway HTTP API (2.0) events.

It's enough to drive the Lambda handlers locally; it is not a faithful API Gateway.
"""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

LambdaHandler = Callable[[dict[str, Any], object], dict[str, Any]]


def to_event(method: str, target: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
    url = urlsplit(target)
    query = dict(parse_qsl(url.query, keep_blank_values=True))
    try:
        text, is_b64 = body.decode(), False
    except UnicodeDecodeError:
        text, is_b64 = base64.b64encode(body).decode(), True
    return {
        "version": "2.0",
        "rawPath": url.path,
        "rawQueryString": url.query,
        "queryStringParameters": query or None,
        "headers": {k.lower(): v for k, v in headers.items()},
        "requestContext": {"http": {"method": method, "path": url.path}},
        "body": text,
        "isBase64Encoded": is_b64,
    }


def router(routes: dict[tuple[str, str], LambdaHandler]) -> LambdaHandler:
    """One handler that dispatches on (method, path), like API Gateway routes."""

    def dispatch(event: dict[str, Any], context: object) -> dict[str, Any]:
        key = (event["requestContext"]["http"]["method"], event["rawPath"])
        target = routes.get(key)
        if target is None:
            return {"statusCode": 404, "headers": {}, "body": ""}
        return target(event, context)

    return dispatch


def make_server(handler: LambdaHandler, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            event = to_event(self.command, self.path, dict(self.headers.items()), body)
            response = handler(event, None)
            payload = response.get("body") or ""
            data = (
                base64.b64decode(payload)
                if response.get("isBase64Encoded")
                else str(payload).encode()
            )
            self.send_response(int(response["statusCode"]))
            for name, value in (response.get("headers") or {}).items():
                self.send_header(name, value)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _dispatch
        do_POST = _dispatch

        def log_message(self, format: str, *args: Any) -> None:
            # Path only: query strings carry OAuth codes, state and connect-link signatures.
            path = urlsplit(self.path).path
            print(f"  {self.command} {path} -> {args[1] if len(args) > 1 else ''}")

    return ThreadingHTTPServer((host, port), Handler)


def serve_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
