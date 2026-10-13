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
import signal
import sys

from ws_transport import (
    recv_http_headers, parse_http_request, is_websocket_upgrade,
    do_ws_handshake, recv_ws_frame, send_json, send_ws_close
)
from poker_core import Table
from casino import Casino
from accounts import Accounts

DEFAULT_PORT = int(os.environ.get("PORT", 8000))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
SERVER_TABLES = max(1, int(os.environ.get("POKER_SERVER_TABLES", 1)))  # постоянные столы от сервера
SERVER_TABLE_MIN_BUYIN = 400   # вход за серверный стол (20 больших блайндов)
AUTOSTART_DELAY = 6.0  # сколько секунд ждать после конца раздачи, прежде чем начать следующую

# Админ-команды (выдать/забрать себе фишки за столом для тестов) разрешены
# ТОЛЬКО с этих IP-адресов. Задаётся переменной окружения POKER_ADMIN_IPS
# через запятую. Если переменная пуста/не задана — админ-команды отключены
# для всех, независимо от адреса.
# админ-команды будут работать ТОЛЬКО с этих IP, даже если PIN верный.
# Пусто/не задано = проверка по IP отключена (только PIN).
ADMIN_ALLOWED_IPS = {
    ip.strip() for ip in os.environ.get("POKER_ADMIN_IPS", "").split(",") if ip.strip()
}

MIME_OVERRIDES = {
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
}


def resolve_client_ip(headers, addr):
    """Определяет реальный IP клиента. За обратным прокси (например, Render)
    настоящий IP приходит в заголовке X-Forwarded-For — берём первый адрес
    из него; иначе используем адрес самого TCP-соединения."""
    xff = headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return addr[0]


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
    def __init__(self, server, sock, addr, headers=None):
        super().__init__(daemon=True)
        self.server = server
        self.sock = sock
        self.addr = addr
        self.pid = f"{addr[0]}:{addr[1]}:{time.time()}"
        self.table_code = None
        self.alive = True
        self.client_ip = resolve_client_ip(headers or {}, addr)
        self.account = None   # заполняется после входа (auth_login / auth_register / auth_token)

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
        # --- аккаунты, банк, таблица лидеров ---
        if mtype and mtype.startswith("auth_"):
            self.server.handle_auth(self, msg)
            return
        if mtype in ("leaderboard", "account_get", "claim_bonus"):
            self.server.handle_account(self, msg)
            return
        # --- казино: рулетка, три топора, блэкджек, автомат ---
        if mtype and mtype.startswith("casino_"):
            self.server.casino.handle(self, msg)
            return
        if mtype in ("create_room", "join_room", "join_server", "join_random", "rebuy") \
                and self.account is None:
            self.send({"type": "auth_required"})
            return
        if mtype == "create_room":
            code = self.server.create_room(
                self.pid,
                min_buyin=msg.get("min_buyin", 0),
                max_buyin=msg.get("max_buyin", 0),
                small_blind=msg.get("small_blind"),
                big_blind=msg.get("big_blind"),
                public=not msg.get("private", True),
            )
            ok, reason = self.server.join_room(code, self.pid, self.account.username,
                                               msg.get("chips", 0), self)
            if ok:
                self.table_code = code
                self.send({"type": "room_created", "code": code})
                self.server.broadcast_state(code)
            else:
                self.server.discard_empty_room(code)
                self.send({"type": "error", "message": reason})
        elif mtype in ("join_room", "join_server", "join_random"):
            if mtype == "join_room":
                code = (msg.get("code") or "").strip().upper()
            elif mtype == "join_server":
                code = self.server.pick_server_table()
            else:
                code = self.server.pick_random_table()
            ok, reason = self.server.join_room(code, self.pid, self.account.username,
                                               msg.get("chips", 0), self)
            if ok:
                self.table_code = code
                self.send({"type": "joined", "code": code})
                self.server.broadcast_state(code)
            else:
                self.send({"type": "error", "message": reason})
        elif mtype == "action":
            self.server.player_action(self.table_code, self.pid, msg.get("action"), msg.get("amount", 0))
        elif mtype == "start_hand":
            self.server.try_start_hand(self.table_code)
        elif mtype == "rebuy":
            self.server.player_rebuy(self.table_code, self.pid, msg.get("amount", 0))
        elif mtype == "admin_verify":
            ip_ok = bool(ADMIN_ALLOWED_IPS) and self.client_ip in ADMIN_ALLOWED_IPS
            self.send({"type": "admin_verify_result", "ok": ip_ok,
                       "reason": None if ip_ok else "ip_not_allowed",
                       "your_ip": self.client_ip})
        elif mtype == "admin_set_chips":
            ip_ok = bool(ADMIN_ALLOWED_IPS) and self.client_ip in ADMIN_ALLOWED_IPS
            if ip_ok and self.table_code:
                self.server.admin_set_chips(self.table_code, self.pid, msg.get("amount", 0))
        elif mtype == "admin_set_bank":
            ip_ok = bool(ADMIN_ALLOWED_IPS) and self.client_ip in ADMIN_ALLOWED_IPS
            if ip_ok and self.account:
                try:
                    self.account.set_bank(max(0, int(msg.get("amount", 0))))
                    self.account.push()
                except (TypeError, ValueError):
                    pass
        elif mtype == "leave":
            self.disconnect()
        elif mtype == "ping":
            self.send({"type": "pong"})

    def attach(self, acc):
        if self.account is not None and self.account is not acc:
            self.account.handlers.discard(self)
        self.account = acc
        acc.handlers.add(self)

    def disconnect(self):
        if not self.alive:
            return
        self.alive = False
        self.server.casino.leave(self)
        if self.table_code and self.pid:
            self.server.leave_room(self.table_code, self.pid)
        if self.account is not None:
            self.account.handlers.discard(self)
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
        self.accounts = Accounts()
        self.casino = Casino()
        self.server_counter = 0

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.listen(32)
        self.running = True
        self.casino.start()
        for _ in range(SERVER_TABLES):
            self._new_server_table(base=True)
        threading.Thread(target=self._housekeeping, daemon=True).start()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _housekeeping(self):
        while self.running:
            time.sleep(3)
            self.accounts.flush()

    def stop(self):
        self.running = False
        self.accounts.flush(force=True)
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
            handler = ClientHandler(self, conn, addr, headers=headers)
            handler.run()  # блокирующий цикл именно в этом потоке
            return

        self._serve_static(conn, path)

    def _serve_static(self, conn, path):
        if path == "/":
            path = "/menu.html"   # главное меню казино; покер — /index.html
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

    def create_room(self, host_pid, min_buyin=0, max_buyin=0, small_blind=None, big_blind=None, public=False):
        with self.lock:
            while True:
                code = gen_code()
                if code not in self.tables:
                    break
            self.tables[code] = Table(code, host_pid, small_blind=small_blind,
                                      big_blind=big_blind, min_buyin=min_buyin, max_buyin=max_buyin)
            self.handlers[code] = {}
            self.table_meta[code] = {"public": bool(public), "server": False, "base": False}
            return code

    # ---------- серверные столы (не требуют создания) ----------

    def _new_server_table(self, base=False):
        with self.lock:
            while True:
                self.server_counter += 1
                code = f"SRV{self.server_counter}"
                if code not in self.tables:
                    break
            self.tables[code] = Table(code, "server", min_buyin=SERVER_TABLE_MIN_BUYIN)
            self.handlers[code] = {}
            self.table_meta[code] = {"public": True, "server": True, "base": base}
            return code

    def _free(self, code):
        t = self.tables.get(code)
        return bool(t) and len(t.players) < t.MAX_PLAYERS

    def pick_server_table(self):
        """Серверный стол: садимся туда, где уже есть люди (чтобы началась игра);
        если все серверные столы полны — сервер сам открывает ещё один."""
        with self.lock:
            cand = [c for c, m in self.table_meta.items() if m.get("server") and self._free(c)]
            if not cand:
                return self._new_server_table()
            return max(cand, key=lambda c: (len(self.tables[c].players), -int(c[3:])))

    def pick_random_table(self):
        """Случайный стол среди серверных и публичных. Предпочитаем те, где уже кто-то сидит."""
        with self.lock:
            cand = [c for c, m in self.table_meta.items() if m.get("public") and self._free(c)]
            if not cand:
                return self._new_server_table()
            with_people = [c for c in cand if self.tables[c].players]
            return random.choice(with_people or cand)

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
            acct = getattr(handler, "account", None)
            if acct is None:
                return False, "Сначала войдите в аккаунт"
            if acct.seat_code:
                return False, "Вы уже сидите за другим столом"
            try:
                chips = int(chips)
            except (TypeError, ValueError):
                chips = 0
            if chips <= 0:
                return False, "Укажите, сколько фишек занести за стол"
            if chips > acct.bank:
                return False, "В банке не хватает фишек"
            ok, reason = table.add_player(pid, acct.username, chips)
            if not ok:
                return False, reason
            acct.add(-chips)           # фишки переезжают из банка за стол
            acct.staked = chips
            acct.seat_code = code
            acct.push()
            self.handlers.setdefault(code, {})[pid] = handler
            self._maybe_autostart(code)
            return True, "ok"

    def leave_room(self, code, pid):
        with self.lock:
            table = self.tables.get(code)
            if not table:
                return
            player = table.players.get(pid)
            stack = player.chips if player else 0
            h = self.handlers.get(code, {}).get(pid)
            table.remove_player(pid)
            if code in self.handlers and pid in self.handlers[code]:
                del self.handlers[code][pid]
            acct = getattr(h, "account", None) if h else None
            if acct is not None and player is not None:
                acct.add(stack)        # остаток стека возвращается в банк аккаунта
                acct.staked = 0
                acct.seat_code = None
                acct.push()
            if not table.players:
                meta = self.table_meta.get(code, {})
                if meta.get("base"):   # основной серверный стол живёт всегда — просто сбрасываем
                    self.tables[code] = Table(code, "server", min_buyin=SERVER_TABLE_MIN_BUYIN)
                    self.handlers[code] = {}
                    return
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
            h = self.handlers.get(code, {}).get(pid)
            acct = getattr(h, "account", None) if h else None
            if acct is None:
                return
            try:
                amount = int(amount)
            except (TypeError, ValueError):
                amount = 0
            if amount <= 0:
                return
            if amount > acct.bank:
                h.send({"type": "error", "message": "В банке не хватает фишек"})
                return
            acct.add(-amount)
            acct.staked += amount
            acct.push()
            player.chips += amount
            if player.chips > 0:
                player.sitting_out = False
            self.broadcast_state(code)
            self._maybe_autostart(code)

    def admin_set_chips(self, code, pid, amount):
        """Только для разрешённых IP (POKER_ADMIN_IPS): жёстко выставляет стек
        за столом (для тестирования — обходит обычные правила бай-ина)."""
        with self.lock:
            table = self.tables.get(code)
            if not table:
                return
            player = table.players.get(pid)
            if not player:
                return
            try:
                amount = max(0, int(amount))
            except (TypeError, ValueError):
                return
            player.chips = amount
            if player.chips > 0:
                player.sitting_out = False
            table.log.append(f"(admin) стек {player.name} установлен: {amount}")
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

    # ---------- аккаунты, бонус, таблица лидеров ----------

    def handle_auth(self, h, msg):
        t = msg.get("type")
        tok = None
        if t == "auth_register":
            acc, tok, err = self.accounts.register(msg.get("username"), msg.get("password"), h.client_ip)
        elif t == "auth_login":
            acc, tok, err = self.accounts.login(msg.get("username"), msg.get("password"), h.client_ip)
        elif t == "auth_token":
            tok = msg.get("token")
            acc = self.accounts.by_token_get(tok)
            err = None if acc else "Сессия устарела, войдите снова"
        elif t == "auth_logout":
            self.accounts.logout(msg.get("token"))
            if h.account is not None:
                h.account.handlers.discard(h)
                h.account = None
            h.send({"type": "auth_logged_out"})
            return
        else:
            return
        if err:
            h.send({"type": "auth_error", "message": err, "code": "bad_token" if t == "auth_token" else "denied"})
            return
        h.attach(acc)
        h.send({"type": "auth_ok", "token": tok, "account": acc.public()})

    def handle_account(self, h, msg):
        t = msg.get("type")
        if t == "leaderboard":
            extra = self.casino.at_stake()
            with self.lock:
                for code, table in self.tables.items():
                    for pid, player in table.players.items():
                        hh = self.handlers.get(code, {}).get(pid)
                        acc = getattr(hh, "account", None) if hh else None
                        if acc is not None:
                            k = acc.username.lower()
                            extra[k] = extra.get(k, 0) + player.chips
            rows = self.accounts.leaderboard(extra)
            out = [{"rank": i + 1, "username": n, "chips": c} for i, (n, c) in enumerate(rows[:50])]
            you = None
            if h.account is not None:
                for i, (n, c) in enumerate(rows):
                    if n == h.account.username:
                        you = {"rank": i + 1, "username": n, "chips": c}
                        break
            h.send({"type": "leaderboard", "rows": out, "you": you, "total": len(rows)})
            return
        if h.account is None:
            h.send({"type": "auth_required"})
            return
        if t == "claim_bonus":
            ok = self.accounts.claim_bonus(h.account)
            h.send({"type": "bonus_result", "ok": ok})
        h.account.push()


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
    def _graceful(*_):
        srv.stop()          # сохраняет аккаунты на диск
        sys.exit(0)
    try:
        signal.signal(signal.SIGTERM, _graceful)
    except (ValueError, OSError):
        pass
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    run_server_blocking()
