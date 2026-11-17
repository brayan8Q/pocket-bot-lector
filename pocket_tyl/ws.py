"""A small asyncio WebSocket client (RFC 6455) on the standard library: text and binary messages, ping/pong, close.
Enough for Pocket Option's Socket.IO endpoint; no extra package to install."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Closed(Exception):
    pass


def frame(opcode: int, payload: bytes, mask: bool = True) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    bit = 0x80 if mask else 0
    if n < 126:
        head += bytes([bit | n])
    elif n < 65536:
        head += bytes([bit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([bit | 127]) + struct.pack("!Q", n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    b1, b2 = await reader.readexactly(2)
    fin, opcode, masked, n = bool(b1 & 0x80), b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", await reader.readexactly(8))[0]
    key = await reader.readexactly(4) if masked else None
    data = await reader.readexactly(n)
    if key:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return fin, opcode, data


class WebSocket:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.lock = asyncio.Lock()

    @classmethod
    async def connect(cls, url: str, headers: dict[str, str] | None = None, timeout: float = 15.0) -> "WebSocket":
        u = urlsplit(url)
        secure = u.scheme == "wss"
        port = u.port or (443 if secure else 80)
        context = ssl.create_default_context() if secure else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(u.hostname, port, ssl=context, server_hostname=u.hostname if secure else None), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        lines = [f"GET {path} HTTP/1.1", f"Host: {u.hostname}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        first = status.split(b"\r\n", 1)[0]
        if b" 101 " not in first + b" ":
            writer.close()
            raise ConnectionError(f"El servidor no aceptó el WebSocket: {first.decode(errors='replace')}")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest())
        if accept not in status:
            writer.close()
            raise ConnectionError("Respuesta de WebSocket inválida.")
        return cls(reader, writer)

    async def send(self, message: str | bytes) -> None:
        data = message.encode() if isinstance(message, str) else message
        async with self.lock:
            self.writer.write(frame(0x1 if isinstance(message, str) else 0x2, data))
            await self.writer.drain()

    async def recv(self) -> str | bytes:
        parts, kind = [], None
        while True:
            fin, opcode, data = await read_frame(self.reader)
            if opcode == 0x8:
                raise Closed("El servidor cerró la conexión.")
            if opcode == 0x9:  # ping → pong
                async with self.lock:
                    self.writer.write(frame(0xA, data))
                    await self.writer.drain()
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                kind = opcode
            parts.append(data)
            if fin:
                payload = b"".join(parts)
                return payload.decode("utf-8", errors="replace") if kind == 0x1 else payload

    async def close(self) -> None:
        try:
            async with self.lock:
                self.writer.write(frame(0x8, b""))
                await self.writer.drain()
        except Exception:
            pass
        self.writer.close()
