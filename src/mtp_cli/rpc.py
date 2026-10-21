"""Minimal client for the writable.site "Datapages" RPC protocol.

The tracker has no REST API: the web app talks JSON over a single WebSocket.

Wire format (reverse-engineered from the site's JS bundle):
  request   [seq, ["Datapages API v0.0", op], *args]
  response  [-seq, error_or_null, result]
  push      [seq, ["Datapages API v0.0", "notify"], view_key, event]   (seq 0 = no reply wanted)
  ping      [seq, "_internal_ping", null]                              (server liveness check)

Queries are "streaming views": streamingView(key, filter) makes the server push
{"event": "here", pageId, data} for each match, then {"event": "waiting"} once the
initial result set is complete.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from typing import Any

import websockets

DEFAULT_SERVER = "wss://b2.writable.site/_API"
ORIGIN = "https://writable.site"
API = "Datapages API v0.0"


class RPCError(RuntimeError):
    pass


class Client:
    def __init__(self, server: str = DEFAULT_SERVER, timeout: float = 60.0):
        self.server = server
        self.timeout = timeout
        self._seq = itertools.count(1)
        self._view_seq = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._views: dict[str, asyncio.Queue] = {}
        self._ws = None
        self._reader_task = None
        self.user: dict | None = None

    async def __aenter__(self) -> "Client":
        self._ws = await websockets.connect(self.server, origin=ORIGIN, max_size=2**28)
        self._reader_task = asyncio.create_task(self._reader())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._ws:
            await self._ws.close()

    async def _reader(self) -> None:
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    continue
                msg = json.loads(raw)
                seq = msg[0]
                if seq < 0:
                    fut = self._pending.pop(-seq, None)
                    if fut and not fut.done():
                        err = msg[1]
                        if err:
                            fut.set_exception(RPCError((err or {}).get("message") or str(err)))
                        else:
                            fut.set_result(msg[2] if len(msg) > 2 else None)
                    continue
                name = msg[1]
                result = None
                if isinstance(name, list) and name[-1] == "notify":
                    queue = self._views.get(msg[2])
                    if queue is not None:
                        queue.put_nowait(msg[3])
                elif name == "_internal_ping":
                    result = int(time.time() * 1000)
                if seq > 0:
                    await self._ws.send(json.dumps([-seq, None, result]))
        except websockets.ConnectionClosed as e:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RPCError(f"connection closed: {e}"))

    async def call(self, op: str, *args: Any) -> Any:
        seq = next(self._seq)
        fut = asyncio.get_running_loop().create_future()
        self._pending[seq] = fut
        await self._ws.send(json.dumps([seq, [API, op], *args]))
        try:
            return await asyncio.wait_for(fut, self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(seq, None)
            raise RPCError(f"timed out waiting for {op}") from None

    async def login(self, username: str, password: str) -> dict:
        res = await self.call("login", username, password)
        self.user = res["user"]
        return self.user

    async def read(self, page_id: str) -> dict | None:
        return await self.call("read", page_id)

    async def add(self, data: dict) -> str:
        """Create a new page under the user's posts folder; returns its page id."""
        return await self.call("add", data)

    async def query(self, flt: dict) -> dict[str, dict]:
        """Run a one-shot query; returns {pageId: data}."""
        key = f"cli{next(self._view_seq)}"
        queue: asyncio.Queue = asyncio.Queue()
        self._views[key] = queue
        out: dict[str, dict] = {}
        try:
            await self.call("streamingView", key, flt)
            while True:
                ev = await asyncio.wait_for(queue.get(), self.timeout)
                kind = ev.get("event")
                if kind == "here":
                    out[ev["pageId"]] = ev["data"]
                elif kind == "gone":
                    out.pop(ev["pageId"], None)
                elif kind in ("waiting", "end"):
                    break
                elif kind == "error":
                    raise RPCError(f"query {flt!r} failed: {ev}")
        finally:
            self._views.pop(key, None)
            try:
                await self.call("stopStreamingView", key)
            except RPCError:
                pass
        return out
