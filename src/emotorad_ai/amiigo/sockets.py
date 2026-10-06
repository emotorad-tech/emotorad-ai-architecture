"""The open chat sockets of this process, by rider (docs/contracts/amiigo-support-chat.md,
"The chat socket", "Tickets and Zoho Desk").

A rider may have a socket open on more than one device; each is one
`OpenSocket`, added when its `ready` goes out and removed when it closes
(amiigo/socket.py). A frame for the rider rather than for one socket (a
ticket closed in Zoho Desk: `ticket_update`, the plan's Task 6) goes to every
socket the rider has open, through `SocketRegistry.push`, from any thread.

Open sockets are held by this process only: with more than one container, a
push needs a shared channel to reach the container holding the socket (the
contract's "For the server team"). Staging runs one.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Optional


class OpenSocket:
    """One open chat socket. Frames go out one at a time, whoever sends
    them: the socket's own loop, a turn finishing, or another thread through
    `push`. A send to a socket that has gone is dropped, and logged once."""

    def __init__(self, websocket: Any, rider_hash: str, loop: asyncio.AbstractEventLoop, log: Any = None) -> None:
        self._websocket = websocket
        self.rider_hash = rider_hash
        self._loop = loop
        self._log = log
        self._lock = asyncio.Lock()
        self.closed = False

    async def send(self, frame: Dict[str, Any]) -> bool:
        """Whether the frame went out."""
        async with self._lock:
            if self.closed:
                return False
            try:
                await self._websocket.send_json(frame)
            except Exception as exc:
                # The app went away mid-send: nothing more can reach it. The
                # class only; a message can carry the frame.
                self._gone(type(exc).__name__)
                return False
            return True

    def _gone(self, error: Optional[str]) -> None:
        if not self.closed and error is not None and self._log is not None:
            self._log.emit("amiigo_frame_not_sent", "amiigo", rider_hash=self.rider_hash, error=error)
        self.closed = True

    async def close(self, code: int, reason: str) -> None:
        """Close with a code and reason, after any frame going out; later
        frames are dropped quietly."""
        async with self._lock:
            if self.closed:
                return
            self.closed = True
            try:
                await self._websocket.close(code=code, reason=reason)
            except Exception as exc:
                # The app left first: there is no one to tell.
                if self._log is not None:
                    self._log.emit("amiigo_frame_not_sent", "amiigo", rider_hash=self.rider_hash,
                                   error=type(exc).__name__)

    def mark_closed(self) -> None:
        """The socket has closed: later frames are dropped quietly."""
        self.closed = True

    def push(self, frame: Dict[str, Any]) -> bool:
        """Send from any thread, without waiting for it to go out. False when
        the socket's loop has stopped."""
        try:
            running: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._loop.create_task(self.send(frame))
            return True
        try:
            asyncio.run_coroutine_threadsafe(self.send(frame), self._loop)
        except RuntimeError:
            # The loop is closed: the socket went with it.
            self._gone(None)
            return False
        return True


class SocketRegistry:
    """Every open socket of this process, by the rider's user key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: Dict[str, List[OpenSocket]] = {}

    def add(self, user_key: str, socket: OpenSocket) -> None:
        with self._lock:
            self._open.setdefault(user_key, []).append(socket)

    def remove(self, user_key: str, socket: OpenSocket) -> None:
        with self._lock:
            mine = self._open.get(user_key, [])
            if socket in mine:
                mine.remove(socket)
            if not mine:
                self._open.pop(user_key, None)

    def of(self, user_key: str) -> List[OpenSocket]:
        with self._lock:
            return list(self._open.get(user_key, []))

    def push(self, user_key: str, frame: Dict[str, Any]) -> int:
        """Send the frame to every open socket of the rider, from any
        thread. How many it was handed to."""
        return sum(1 for socket in self.of(user_key) if not socket.closed and socket.push(frame))

    def __len__(self) -> int:
        with self._lock:
            return sum(len(sockets) for sockets in self._open.values())
