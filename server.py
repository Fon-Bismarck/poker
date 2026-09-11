# -*- coding: utf-8 -*-
"""
Сервер покера: одновременно
  1) отдаёт веб-страницу игры (HTML/CSS/JS) по обычному HTTP,
  2) обслуживает игровые WebSocket-соединения на пути /ws.

Один и тот же сервер обслуживает и компьютеры, и телефоны — все заходят
браузером на один и тот же адрес (http://IP-хоста:8000), поэтому играть
можно кроссплатформенно за одним столом.
"""

import os
import socket
import threading
import random
import string
import time
import mimetypes

from ws_transport import (
    recv_http_headers, parse_http_request, is_websocket_upgrade,
    do_ws_handshake, recv_ws_frame, send_json, send_ws_close
)
from poker_core import Table

DEFAULT_PORT = int(os.environ.get("PORT", 8000))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
AUTOSTART_DELAY = 6.0  # сколько секунд ждать после конца раздачи, прежде чем начать следующую

MIME_OVERRIDES = {
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
}


def gen_code(length=5):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


# ---------------------- Игровая часть (комнаты/столы) ----------------------

class ClientHandler(threading.Thread):
    def __init__(self, server, sock, addr):
        super().__init__(daemon=True)
        self.server = server
        self.sock = sock
        self.addr = addr
        self.pid = f"{addr[0]}:{addr[1]}:{time.time()}"
        self.table_code = None
        self.alive = True

    def send(self, obj):
        send_json(self.sock, obj)

    def run(self):
        try:
            while self.alive:
                opcode, payload = recv_ws_frame(self.sock)
                if opcode is None:
                    break
                if opcode == 0x8:  # close
                    break
                if opcode == 0x1:  # текст
                    try:
                        import json
                        msg = json.loads(payload.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    self.handle(msg)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            self.disconnect()

    def handle(self, msg):
        mtype = msg.get("type")
        if mtype == "create_room":
            code = self.server.create_room(
                self.pid,
                background=msg.get("background"),
                min_buyin=msg.get("min_buyin", 0),
                small_blind=msg.get("small_blind"),
                big_blind=msg.get("big_blind"),
            )
            self.table_code = code
            ok, reason = self.server.join_room(code, self.pid, msg.get("name", "Игрок"),
                                                msg.get("chips", 1000), self)
            if ok:
                self.send({"type": "room_created", "code": code,
                           "background": self.server.get_background(code)})
                self.server.broadcast_state(code)
            else:
                self.server.discard_empty_room(code)
                self.send({"type": "error", "message": reason})

        elif mtype == "join_room":
            code = (msg.get("code") or "").strip().upper()
            self.table_code = code
            ok, reason = self.server.join_room(code, self.pid, msg.get("name", "Игрок"),
                                                msg.get("chips", 1000), self)
            if ok:
                self.send({"type": "joined", "code": code,
                           "background": self.server.get_background(code)})
                self.server.broadcast_state(code)
            else:
                self.send({"type": "error", "message": reason})

        elif mtype == "action":
            self.server.player_action(self.table_code, self.pid, msg.get("action"), msg.get("amount", 0))

        elif mtype == "start_hand":
            self.server.try_start_hand(self.table_code)

        elif mtype == "rebuy":
            self.server.player_rebuy(self.table_code, self.pid, msg.get("amount", 0))

        elif mtype == "leave":
            self.disconnect()

        elif mtype == "ping":
            self.send({"type": "pong"})

    def disconnect(self):
        if not self.alive:
            return
        self.alive = False
        if self.table_code and self.pid:
            self.server.leave_room(self.table_code, self.pid)
        try:
            send_ws_close(self.sock)
            self.sock.close()
        except OSError:
            pass


class PokerServer:
    def __init__(self, port=DEFAULT_PORT):
        self.port = port
        self.tables = {}
        self.table_meta = {}   # code -> {"background": ..., "min_buyin": ..., "small_blind":..., "big_blind":...}
        self.handlers = {}
        self.lock = threading.RLock()
        self.sock = None
        self.running = False

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.listen(32)
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

    def _accept_loop(self):
        while self.running:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_connection, args=(conn, addr), daemon=True).start()

    def _handle_connection(self, conn, addr):
        raw = recv_http_headers(conn)
        if raw is None:
            conn.close()
            return
        method, path, headers = parse_http_request(raw)
        path = (path or "/").split("?")[0]

        if path == "/ws" and is_websocket_upgrade(headers):
            do_ws_handshake(conn, headers)
            handler = ClientHandler(self, conn, addr)
            handler.run()  # блокирующий цикл именно в этом потоке
            return

        self._serve_static(conn, path)

    def _serve_static(self, conn, path):
        if path == "/":
            path = "/index.html"
        safe_path = os.path.normpath(path).lstrip(os.sep)
        full_path = os.path.join(WEB_DIR, safe_path)

        if not os.path.abspath(full_path).startswith(os.path.abspath(WEB_DIR)):
            self._send_http(conn, 403, b"Forbidden", "text/plain")
            return

        if not os.path.isfile(full_path):
            self._send_http(conn, 404, b"Not found", "text/plain")
            return

        ext = os.path.splitext(full_path)[1]
        content_type = MIME_OVERRIDES.get(ext) or mimetypes.guess_type(full_path)[0] or "application/octet-stream"
        with open(full_path, "rb") as f:
            body = f.read()
        self._send_http(conn, 200, body, content_type)

    def _send_http(self, conn, status, body, content_type):
        status_text = {200: "OK", 403: "Forbidden", 404: "Not Found"}.get(status, "OK")
        header = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        try:
            conn.sendall(header + body)
        except OSError:
            pass
        finally:
            conn.close()

    # ---------- логика столов (идентична предыдущей версии) ----------

    def create_room(self, host_pid, background=None, min_buyin=0, small_blind=None, big_blind=None):
        with self.lock:
            while True:
                code = gen_code()
                if code not in self.tables:
                    break
            self.tables[code] = Table(code, host_pid, small_blind=small_blind,
                                       big_blind=big_blind, min_buyin=min_buyin)
            self.handlers[code] = {}
            self.table_meta[code] = {"background": background}
            return code

    def get_background(self, code):
        return self.table_meta.get(code, {}).get("background")

    def discard_empty_room(self, code):
        with self.lock:
            table = self.tables.get(code)
            if table and not table.players:
                del self.tables[code]
                self.handlers.pop(code, None)
                self.table_meta.pop(code, None)

    def join_room(self, code, pid, name, chips, handler):
        with self.lock:
            table = self.tables.get(code)
            if not table:
                return False, "Стол с таким кодом не найден"
            ok, reason = table.add_player(pid, name, chips)
            if not ok:
                return False, reason
            self.handlers.setdefault(code, {})[pid] = handler
            self._maybe_autostart(code)
            return True, "ok"

    def leave_room(self, code, pid):
        with self.lock:
            table = self.tables.get(code)
            if not table:
                return
            table.remove_player(pid)
            if code in self.handlers and pid in self.handlers[code]:
                del self.handlers[code][pid]
            if not table.players:
                del self.tables[code]
                del self.handlers[code]
                self.table_meta.pop(code, None)
                return
            self.broadcast_state(code)

    def player_action(self, code, pid, action, amount):
        with self.lock:
            table = self.tables.get(code)
            if not table:
                return
            ok, reason = table.apply_action(pid, action, amount)
            if ok:
                self.broadcast_state(code)
                self._maybe_autostart(code)
                self._schedule_turn_timeout(code)
            else:
                h = self.handlers.get(code, {}).get(pid)
                if h:
                    h.send({"type": "error", "message": reason})

    def player_rebuy(self, code, pid, amount):
        with self.lock:
            table = self.tables.get(code)
            if not table:
                return
            player = table.players.get(pid)
            if not player:
                return
            try:
                amount = int(amount)
            except (TypeError, ValueError):
                amount = 0
            if amount <= 0:
                return
            player.chips += amount
            if player.chips > 0:
                player.sitting_out = False
            self.broadcast_state(code)
            self._maybe_autostart(code)

    def try_start_hand(self, code):
        with self.lock:
            table = self.tables.get(code)
            if table and table.stage in ("waiting", "showdown") and table.can_start_hand():
                table.start_hand()
                self.broadcast_state(code)
                self._schedule_turn_timeout(code)

    def _maybe_autostart(self, code):
        table = self.tables.get(code)
        if not table:
            return
        if table.stage in ("waiting", "showdown") and table.can_start_hand():
            timer = threading.Timer(AUTOSTART_DELAY, self._delayed_start, args=(code, table.hand_number))
            timer.daemon = True
            timer.start()

    def _delayed_start(self, code, expected_hand_number):
        with self.lock:
            table = self.tables.get(code)
            if not table:
                return
            if table.stage in ("waiting", "showdown") and table.hand_number == expected_hand_number and table.can_start_hand():
                table.start_hand()
                self.broadcast_state(code)
                self._schedule_turn_timeout(code)

    def _schedule_turn_timeout(self, code):
        """Планирует автодействие (чек/фолд), если игрок не походит вовремя."""
        with self.lock:
            table = self.tables.get(code)
            if not table or not table.current_turn or table.turn_deadline is None:
                return
            token = table.turn_token
            delay = max(0.05, table.turn_deadline - time.time())
        timer = threading.Timer(delay, self._handle_turn_timeout, args=(code, token))
        timer.daemon = True
        timer.start()

    def _handle_turn_timeout(self, code, token):
        with self.lock:
            table = self.tables.get(code)
            if not table or table.turn_token != token:
                return  # ход уже сменился раньше, чем сработал таймер
            pid = table.current_turn
            if not pid:
                return
            actions = table.legal_actions(pid)
            if not actions:
                return
            action = "check" if "check" in actions else "fold"
            ok, _ = table.apply_action(pid, action)
            if not ok:
                return
            table.log.append("(время на ход истекло — автодействие)")
            self.broadcast_state(code)
            self._maybe_autostart(code)
            self._schedule_turn_timeout(code)

    def broadcast_state(self, code):
        table = self.tables.get(code)
        if not table:
            return
        for pid, h in list(self.handlers.get(code, {}).items()):
            state = table.public_state(for_pid=pid)
            h.send({"type": "state", "state": state})


def run_server_blocking(port=DEFAULT_PORT):
    srv = PokerServer(port)
    srv.start()
    print(f"Сервер покера запущен на порту {port}")
    if os.environ.get("PORT"):
        # Запущено в облаке (например, Render) — там свой публичный адрес,
        # локальный IP этой машины никого не интересует.
        print("Сервер работает в облаке. Ссылка для игроков — та, что выдал хостинг.")
    else:
        ip = get_local_ip()
        print(f"Откройте в браузере на этом компьютере: http://localhost:{port}")
        print(f"Друзья в этой же Wi-Fi/локальной сети открывают: http://{ip}:{port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    run_server_blocking()
