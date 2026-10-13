# -*- coding: utf-8 -*-
"""
accounts.py — аккаунты игроков, банк фишек, дневной бонус и таблица лидеров.

Данные хранятся в JSON-файле (по умолчанию data/accounts.json рядом с сервером,
папку можно сменить переменной окружения POKER_DATA_DIR). Пароли хранятся
только в виде PBKDF2-хэша с солью.

ВАЖНО для бесплатного Render: файловая система там временная — при перезапуске
или передеплое аккаунты пропадут. Для постоянного хранения подключите диск
(Render Disks) и укажите POKER_DATA_DIR на него, либо запускайте у себя/на VPS.
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time

START_BANK = 1000
BONUS_AMOUNT = 1000
BONUS_COOLDOWN = 24 * 60 * 60          # секунд
MAX_TOKENS = 6
NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_\-]{3,16}$")
PBKDF2_ROUNDS = 120_000

DATA_DIR = os.environ.get("POKER_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
DATA_FILE = os.path.join(DATA_DIR, "accounts.json")


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt), PBKDF2_ROUNDS).hex()


class Account:
    def __init__(self, username, salt, pw_hash, bank=START_BANK, tokens=None,
                 last_bonus=0, created=None, staked=0):
        self.username = username
        self.salt = salt
        self.pw_hash = pw_hash
        self.bank = int(bank)
        self.tokens = list(tokens or [])
        self.last_bonus = float(last_bonus)    # unix-время последнего бонуса
        self.created = created or time.time()
        self.staked = int(staked)              # фишки, занесённые за покерный стол
        self.seat_code = None                  # (не сохраняется) за каким столом сидит
        self.handlers = set()                  # (не сохраняется) открытые соединения
        self.manager = None

    # --- банк ---
    def set_bank(self, value):
        self.bank = max(0, int(value))
        if self.manager:
            self.manager.dirty = True

    def add(self, delta):
        self.set_bank(self.bank + int(delta))

    def bonus_left(self):
        return max(0.0, self.last_bonus + BONUS_COOLDOWN - time.time())

    def public(self):
        return {"username": self.username, "bank": self.bank,
                "bonus_left": round(self.bonus_left()), "bonus_amount": BONUS_AMOUNT}

    def push(self):
        """Разослать актуальный банк по всем открытым соединениям аккаунта."""
        msg = {"type": "account", "account": self.public()}
        for h in list(self.handlers):
            try:
                h.send(msg)
            except Exception:
                pass

    def to_json(self):
        return {"username": self.username, "salt": self.salt, "pw_hash": self.pw_hash,
                "bank": self.bank, "tokens": self.tokens[-MAX_TOKENS:],
                "last_bonus": self.last_bonus, "created": self.created, "staked": self.staked}


class Accounts:
    def __init__(self):
        self.lock = threading.RLock()
        self.by_name = {}          # lower(username) -> Account
        self.by_token = {}         # token -> Account
        self.dirty = False
        self.fails = {}            # ip -> [timestamps неудачных входов]
        self.load()

    # ---------- хранилище ----------
    def load(self):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {"accounts": []}
        for d in data.get("accounts", []):
            try:
                a = Account(d["username"], d["salt"], d["pw_hash"], d.get("bank", START_BANK),
                            d.get("tokens"), d.get("last_bonus", 0), d.get("created"),
                            d.get("staked", 0))
            except KeyError:
                continue
            # сервер перезапущен -> столы пропали, возвращаем занесённые за стол фишки
            if a.staked:
                a.bank += a.staked
                a.staked = 0
                self.dirty = True
            a.manager = self
            self.by_name[a.username.lower()] = a
            for t in a.tokens:
                self.by_token[t] = a

    def flush(self, force=False):
        with self.lock:
            if not (self.dirty or force):
                return
            self.dirty = False
            payload = {"accounts": [a.to_json() for a in self.by_name.values()]}
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, DATA_FILE)
        except OSError:
            self.dirty = True

    # ---------- вход / регистрация ----------
    def _throttled(self, ip):
        now = time.time()
        lst = [t for t in self.fails.get(ip, []) if now - t < 300]
        self.fails[ip] = lst
        return len(lst) >= 8

    def _fail(self, ip):
        self.fails.setdefault(ip, []).append(time.time())

    def _new_token(self, acc):
        tok = secrets.token_hex(24)
        acc.tokens.append(tok)
        while len(acc.tokens) > MAX_TOKENS:
            self.by_token.pop(acc.tokens.pop(0), None)
        self.by_token[tok] = acc
        self.dirty = True
        return tok

    def register(self, username, password, ip="?"):
        username = (username or "").strip()
        password = password or ""
        if self._throttled(ip):
            return None, None, "Слишком много попыток. Подождите пару минут."
        if not NAME_RE.match(username):
            return None, None, "Имя: 3–16 символов, буквы, цифры, _ и -"
        if len(password) < 4 or len(password) > 64:
            return None, None, "Пароль: от 4 до 64 символов"
        with self.lock:
            if username.lower() in self.by_name:
                return None, None, "Это имя уже занято"
            salt = secrets.token_hex(16)
            acc = Account(username, salt, _hash(password, salt))
            acc.manager = self
            self.by_name[username.lower()] = acc
            tok = self._new_token(acc)
            self.dirty = True
        self.flush()
        return acc, tok, None

    def login(self, username, password, ip="?"):
        if self._throttled(ip):
            return None, None, "Слишком много попыток. Подождите пару минут."
        with self.lock:
            acc = self.by_name.get((username or "").strip().lower())
            ok = bool(acc) and secrets.compare_digest(
                _hash(password or "", acc.salt), acc.pw_hash)
            if not ok:
                self._fail(ip)
                return None, None, "Неверное имя или пароль"
            tok = self._new_token(acc)
        self.flush()
        return acc, tok, None

    def by_token_get(self, token):
        with self.lock:
            return self.by_token.get(token or "")

    def logout(self, token):
        with self.lock:
            acc = self.by_token.pop(token or "", None)
            if acc and token in acc.tokens:
                acc.tokens.remove(token)
                self.dirty = True

    # ---------- бонус ----------
    def claim_bonus(self, acc):
        with self.lock:
            left = acc.bonus_left()
            if left > 0:
                return False
            acc.last_bonus = time.time()
            acc.add(BONUS_AMOUNT)
            return True

    # ---------- таблица лидеров ----------
    def leaderboard(self, extra, limit=50):
        """extra: {username_lower: фишки вне банка (за столами и в ставках)}."""
        with self.lock:
            rows = [(a.username, a.bank + extra.get(a.username.lower(), 0))
                    for a in self.by_name.values()]
        rows.sort(key=lambda r: (-r[1], r[0].lower()))
        return rows

    def count(self):
        return len(self.by_name)
