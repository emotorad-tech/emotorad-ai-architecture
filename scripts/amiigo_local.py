"""Test the Amiigo app's support chat from a browser, the way the app talks to it.

    python scripts/amiigo_local.py                                  # against staging
    python scripts/amiigo_local.py --target http://127.0.0.1:8001   # against a local server

Then open the address it prints, paste an Amiigo staging **access token** (sign
in to the Amiigo staging app with a test number and copy the token it gets),
and chat.

Why a relay: the app sends the rider's token as `Authorization: Bearer` on
every call and on the socket's handshake (docs/contracts/amiigo-support-chat.md).
A browser cannot put a header on a WebSocket handshake, so this small server
on 127.0.0.1 holds the token in memory and opens the real socket to the
target with the header, exactly as the app does, passing frames both ways.
The `/amiigo/v1/...` HTTP calls (history, uploads, deletion requests) go
through it the same way. Photos and videos are PUT from the browser straight
to S3, as the app does, which is why the page runs on http://localhost:8000:
it is the origin the media bucket's CORS allows.

Nothing on the server changes for this, and the token is never printed,
logged, written to disk or sent anywhere but the target. Use test numbers
only. The relay listens on 127.0.0.1 only, and refuses a request from any
other page's origin, so another website open in the same browser cannot use
the token it holds.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import pathlib
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence

# At module level: FastAPI reads these types from the module to tell a
# Request or a WebSocket parameter from a query parameter.
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "amiigo-test.html"
HOST = "127.0.0.1"
PAGE_HOST = "localhost"
DEFAULT_PORT = 8000
DEFAULT_TARGET = "https://ai-release-stage.emotorad.com"
TOKEN_PREFIX = "v4.public."
# Close codes a server may not send itself (1005, 1006, 1015). When the
# target's socket ends with one, the page is told 1011 with the reason.
_UNSENDABLE = (1005, 1006, 1015)


@dataclass
class Relay:
    target: str
    port: int = DEFAULT_PORT
    # One tester per relay: the token they pasted, in memory only.
    token: Optional[str] = field(default=None, repr=False)

    @property
    def socket_url(self) -> str:
        parts = urllib.parse.urlsplit(self.target)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return urllib.parse.urlunsplit((scheme, parts.netloc, "/amiigo/v1/chat", "", ""))

    def allowed_origin(self, origin: Optional[str]) -> bool:
        """The page's own origin only. A request with no Origin (curl, a
        script on this machine) is allowed; a browser always sends one."""
        if origin is None:
            return True
        return origin in ("http://%s:%d" % (PAGE_HOST, self.port), "http://%s:%d" % (HOST, self.port))

    def headers(self) -> Dict[str, str]:
        return {"Authorization": "Bearer %s" % self.token} if self.token else {}


def describe_token(token: str) -> Dict[str, Any]:
    """What the page shows about a pasted token: its kind and expiry, read
    without checking the signature (the target checks it). Never the token,
    and the phone only as its last two digits."""
    if not token.startswith(TOKEN_PREFIX):
        raise ValueError("not a PASETO v4.public token (it should start with %s)" % TOKEN_PREFIX)
    body = token[len(TOKEN_PREFIX):].split(".")[0]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        claims = json.loads(raw[:-64].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("the token's body cannot be read")
    if isinstance(claims, dict) and isinstance(claims.get("payload"), str):
        try:
            claims = dict(claims, **json.loads(claims["payload"]))
        except ValueError:
            pass
    phone = str(claims.get("phone") or "") if isinstance(claims, dict) else ""
    return {
        "token_type": claims.get("token_type") if isinstance(claims, dict) else None,
        "expires": (claims.get("expired_at") or claims.get("exp")) if isinstance(claims, dict) else None,
        "phone_ends": phone[-2:] if len(phone) >= 2 else None,
    }


def build_app(relay: Relay, page: pathlib.Path = PAGE, http_transport: Any = None, upstream_connect: Any = None):
    """The relay. `http_transport` and `upstream_connect` are seams for tests,
    which never open a network connection."""
    import httpx
    from fastapi.responses import FileResponse, JSONResponse, Response

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def refused() -> JSONResponse:
        return JSONResponse({"detail": "origin_not_allowed"}, status_code=403)

    @app.get("/")
    def index():
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/relay/status")
    def status(request: Request):
        if not relay.allowed_origin(request.headers.get("origin")):
            return refused()
        return {"target": relay.target, "token_set": relay.token is not None}

    @app.post("/relay/token")
    async def set_token(request: Request):
        if not relay.allowed_origin(request.headers.get("origin")):
            return refused()
        try:
            token = str((await request.json()).get("token") or "").strip()
            described = describe_token(token)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        relay.token = token
        return dict(described, token_set=True)

    @app.delete("/relay/token")
    def clear_token(request: Request):
        if not relay.allowed_origin(request.headers.get("origin")):
            return refused()
        relay.token = None
        return {"token_set": False}

    @app.api_route("/amiigo/v1/{path:path}", methods=["GET", "POST"])
    async def forward(path: str, request: Request):
        if not relay.allowed_origin(request.headers.get("origin")):
            return refused()
        if relay.token is None:
            # What the target answers with no token, without asking it.
            return JSONResponse({"detail": "token_missing"}, status_code=401)
        url = "%s/amiigo/v1/%s" % (relay.target.rstrip("/"), path)
        if request.url.query:
            url += "?" + request.url.query
        headers = relay.headers()
        if request.headers.get("content-type"):
            headers["Content-Type"] = request.headers["content-type"]
        try:
            async with httpx.AsyncClient(timeout=60, transport=http_transport) as client:
                answer = await client.request(request.method, url, content=await request.body(), headers=headers)
        except httpx.HTTPError as exc:
            return JSONResponse({"detail": "relay_could_not_reach_target", "error": type(exc).__name__}, status_code=502)
        return Response(content=answer.content, status_code=answer.status_code,
                        media_type=answer.headers.get("content-type"))

    @app.websocket("/relay/chat")
    async def chat(ws: WebSocket):
        from websockets.asyncio.client import connect as websockets_connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        connect = upstream_connect or websockets_connect

        if not relay.allowed_origin(ws.headers.get("origin")):
            await ws.close(code=1008, reason="origin_not_allowed")
            return
        await ws.accept()
        if relay.token is None:
            await ws.close(code=4401, reason="token_missing")
            return
        try:
            upstream = await connect(relay.socket_url, additional_headers=relay.headers(),
                                     open_timeout=15, max_size=2 ** 20)
        except InvalidStatus as exc:
            await ws.close(code=1011, reason="target refused the handshake (HTTP %d)" % exc.response.status_code)
            return
        except (OSError, asyncio.TimeoutError) as exc:
            await ws.close(code=1011, reason="relay could not reach the target (%s)" % type(exc).__name__)
            return

        async def browser_to_target():
            try:
                while True:
                    await upstream.send(await ws.receive_text())
            except (WebSocketDisconnect, RuntimeError):
                await upstream.close()
            except ConnectionClosed:
                pass

        async def target_to_browser():
            try:
                async for frame in upstream:
                    await ws.send_text(frame if isinstance(frame, str) else frame.decode("utf-8", "replace"))
            except ConnectionClosed:
                pass
            code = upstream.close_code or 1006
            reason = upstream.close_reason or ""
            if code in _UNSENDABLE:
                code, reason = 1011, "target closed without a code (%d)" % (upstream.close_code or 1006)
            try:
                await ws.close(code=code, reason=reason)
            except RuntimeError:
                pass  # the browser had already gone

        tasks = [asyncio.ensure_future(browser_to_target()), asyncio.ensure_future(target_to_browser())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await upstream.close()

    return app


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test the Amiigo support chat the way the app talks to it.")
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help="the chat server to test (default: staging, %s)" % DEFAULT_TARGET)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="local port (default %d, the origin the media bucket allows uploads from)" % DEFAULT_PORT)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    import uvicorn

    relay = Relay(target=args.target.rstrip("/"), port=args.port)
    print("Target: %s" % relay.target)
    print("Open http://%s:%d/ and paste an Amiigo staging access token (test numbers only)." % (PAGE_HOST, args.port))
    if args.port != DEFAULT_PORT:
        print("Note: photo and video uploads need port %d (the media bucket's allowed origin)." % DEFAULT_PORT)
    print("Stop with Ctrl+C.\n", flush=True)
    # access_log off: paths carry no token, but nothing here needs a log.
    uvicorn.run(build_app(relay), host=HOST, port=args.port, access_log=False, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
