# -*- coding: utf-8 -*-
"""
Небольшая самодостаточная реализация WebSocket-рукопожатия и кадров (frames),
без внешних зависимостей (не нужен pip install websockets).
Поддерживает только текстовые сообщения (JSON) — этого достаточно для игры.
"""

import base64
import hashlib
import struct
import json

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def recv_http_headers(sock):
    """Читает "сырые" HTTP-заголовки запроса (до пустой строки)."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        data += chunk
        if len(data) > 65536:
            return None
    return data.decode("iso-8859-1", errors="replace")


def parse_http_request(raw):
    lines = raw.split("\r\n")
    if not lines:
        return None, None, {}
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return None, None, {}
    method, path = parts[0], parts[1]
    headers = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    return method, path, headers


def is_websocket_upgrade(headers):
    return headers.get("upgrade", "").lower() == "websocket"


def do_ws_handshake(sock, headers):
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode("utf-8")).digest()
    ).decode("utf-8")
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    sock.sendall(response.encode("utf-8"))


def recv_ws_frame(sock):
    """Возвращает (opcode, payload_bytes) или (None, None) при закрытии соединения."""
    head = recv_exact(sock, 2)
    if head is None:
        return None, None
    b1, b2 = head[0], head[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F

    if length == 126:
        ext = recv_exact(sock, 2)
        if ext is None:
            return None, None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = recv_exact(sock, 8)
        if ext is None:
            return None, None
        length = struct.unpack(">Q", ext)[0]

    mask_key = None
    if masked:
        mask_key = recv_exact(sock, 4)
        if mask_key is None:
            return None, None

    payload = recv_exact(sock, length) if length > 0 else b""
    if payload is None:
        return None, None

    if masked and payload:
        payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(len(payload)))

    return opcode, payload


def send_ws_text(sock, text):
    payload = text.encode("utf-8")
    length = len(payload)
    header = bytearray()
    header.append(0x81)  # FIN=1, opcode=1 (текст)
    if length <= 125:
        header.append(length)
    elif length <= 65535:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    try:
        sock.sendall(bytes(header) + payload)
    except OSError:
        pass


def send_ws_close(sock):
    try:
        sock.sendall(b"\x88\x00")
    except OSError:
        pass


def send_json(sock, obj):
    send_ws_text(sock, json.dumps(obj, ensure_ascii=False))
