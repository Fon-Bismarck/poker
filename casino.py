# -*- coding: utf-8 -*-
"""
casino.py — казино-игры поверх вашего сервера покера.

Игры:
  roulette  — европейская рулетка (круглое колесо, 37 ячеек)
  axes      — «Три топора»: квадратная доска 4x3, три топора летят в три разные клетки
  blackjack — блэкджек до 5 игроков за столом против дилера
  slots     — игровой автомат «ручка»: три символа, награда зависит от их ценности

Подбор стола: игрок заходит в игру, сервер сам выбирает стол с НАИМЕНЬШИМ числом
игроков среди незаполненных. Если все столы полны — создаётся новый. Благодаря
этому столы заполняются равномерно (3 игрока -> 3 стола по одному, и т. д. до
заполнения). Пустые лишние столы удаляются.

Фишки берутся прямо из банка аккаунта (accounts.py) — это игровые фишки без денег.
"""
import math
import secrets
import threading
import time
import traceback
from itertools import combinations

RNG = secrets.SystemRandom()
TICK = 0.2
MAX_SPOT_BET = 100000


def clamp_int(v, lo, hi, default=0):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def safe_send(handler, obj):
    try:
        handler.send(obj)
    except Exception:
        pass


class Player:
    def __init__(self, pid, account, handler, seat):
        self.pid = pid
        self.account = account
        self.name = account.username
        self.handler = handler
        self.seat = seat
        # рулетка / топоры
        self.bets = {}
        self.last_bets = {}
        self.pending = 0
        self.last_win = 0
        # блэкджек
        self.bet = 0
        self.last_bet = 0
        self.cards = []
        self.status = "idle"   # idle | waiting | stand | bust | blackjack
        self.result = None     # win | lose | push | blackjack | bust
        self.in_round = False

    @property
    def chips(self):
        """Фишки игрока — это банк его аккаунта (всегда актуальные и сохраняемые)."""
        return self.account.bank

    @chips.setter
    def chips(self, value):
        self.account.set_bank(value)


class BaseTable:
    game = ""
    max_players = 8
    soft_cap = 6   # когда все столы заполнены до этого числа — открывается новый

    def __init__(self, num):
        self.num = num
        self.id = f"{self.game}-{num}"
        self.name = f"Стол {num}"
        self.players = {}
        self.phase = "betting"
        self.phase_total = 0.0
        self.deadline = time.time()

    # ---- общее ----
    def set_phase(self, phase, seconds, t=None):
        t = t if t is not None else time.time()
        self.phase = phase
        self.phase_total = float(seconds)
        self.deadline = t + seconds

    def remaining(self):
        return max(0.0, self.deadline - time.time())

    def free_seat(self):
        used = {p.seat for p in self.players.values()}
        for i in range(self.max_players):
            if i not in used:
                return i
        return None

    def add_player(self, pid, account, handler):
        seat = self.free_seat()
        if seat is None:
            return None
        p = Player(pid, account, handler, seat)
        self.players[pid] = p
        return p

    def err(self, p, message):
        safe_send(p.handler, {"type": "casino_error", "message": message})

    def broadcast(self):
        for p in list(self.players.values()):
            safe_send(p.handler, {"type": "casino_state", "state": self.state_for(p)})

    # ---- переопределяется ----
    def refundable(self, p):
        return p.chips

    def at_stake(self, p):
        """Фишки игрока, которые сейчас лежат на кону (для таблицы лидеров)."""
        return 0

    def remove_player(self, pid):
        p = self.players.get(pid)
        if not p:
            return None
        p.chips = self.refundable(p)
        del self.players[pid]
        return p.chips

    def on_message(self, p, mtype, msg):
        return False

    def tick(self, t):
        return False

    def state_for(self, p):
        return {}


# ============================ общий каркас ставок ============================

class BetTable(BaseTable):
    """Рулетка и «Три топора»: ставки -> розыгрыш -> результат -> снова ставки."""
    BETTING_SEC = 20
    SPIN_SEC = 9
    RESULT_SEC = 6

    def __init__(self, num):
        super().__init__(num)
        self.outcome = None
        self.history = []
        self.round_no = 0
        self.set_phase("betting", self.BETTING_SEC)

    # --- переопределить ---
    def valid_bet(self, kind, value):
        return False

    def draw(self):
        return None

    def settle(self, p):
        return 0

    # --- логика ---
    @staticmethod
    def bets_total(p):
        return sum(p.bets.values())

    def refundable(self, p):
        if self.phase == "betting":
            return p.chips + self.bets_total(p)
        if self.phase == "spin":
            return p.chips + p.pending   # итог уже определён, зачислим сразу
        return p.chips

    def at_stake(self, p):
        if self.phase == "betting":
            return self.bets_total(p)
        if self.phase == "spin":
            return p.pending
        return 0

    def on_message(self, p, mtype, msg):
        if mtype not in ("casino_bet", "casino_clear", "casino_rebet"):
            return False
        if self.phase != "betting":
            self.err(p, "Ставки сейчас не принимаются")
            return False
        if mtype == "casino_bet":
            return self.place(p, msg.get("kind"), msg.get("value"), msg.get("amount"))
        if mtype == "casino_clear":
            return self.clear(p)
        return self.rebet(p)

    def clear(self, p):
        if not p.bets:
            return False
        p.chips += self.bets_total(p)
        p.bets = {}
        return True

    def place(self, p, kind, value, amount):
        kind = str(kind)
        value = clamp_int(value, -1, 10 ** 6, -1)
        if not self.valid_bet(kind, value):
            return False
        amount = clamp_int(amount, 0, MAX_SPOT_BET)
        if amount <= 0:
            return False
        if amount > p.chips:
            self.err(p, "Недостаточно фишек")
            return False
        key = f"{kind}:{value}"
        if p.bets.get(key, 0) + amount > MAX_SPOT_BET:
            self.err(p, "Достигнут лимит ставки на это поле")
            return False
        p.chips -= amount
        p.bets[key] = p.bets.get(key, 0) + amount
        return True

    def rebet(self, p):
        if not p.last_bets:
            return False
        self.clear(p)
        for key, amt in p.last_bets.items():
            if amt <= p.chips:
                p.chips -= amt
                p.bets[key] = amt
        return True

    def tick(self, t):
        if self.phase == "betting":
            if not self.players:           # пустой стол ждёт игроков
                self.deadline = t + self.BETTING_SEC
                self.phase_total = float(self.BETTING_SEC)
                return False
            if t >= self.deadline:
                self.start_spin(t)
                return True
        elif self.phase == "spin":
            if t >= self.deadline:
                self.start_result(t)
                return True
        elif self.phase == "result":
            if t >= self.deadline:
                self.start_betting(t)
                return True
        return False

    def start_spin(self, t):
        self.round_no += 1
        self.outcome = self.draw()
        for p in self.players.values():
            p.pending = self.settle(p) if p.bets else 0
            if p.bets:
                p.last_bets = dict(p.bets)
        self.set_phase("spin", self.SPIN_SEC, t)

    def start_result(self, t):
        for p in self.players.values():
            total_bet = self.bets_total(p)
            p.chips += p.pending
            p.last_win = (p.pending - total_bet) if total_bet else 0
            p.pending = 0
        self.history.append(self.history_item())
        self.history = self.history[-15:]
        self.set_phase("result", self.RESULT_SEC, t)

    def start_betting(self, t):
        for p in self.players.values():
            p.bets = {}
            p.last_win = 0
        self.outcome = None
        self.set_phase("betting", self.BETTING_SEC, t)

    def history_item(self):
        return self.outcome

    def extra_state(self):
        return {}

    def state_for(self, p):
        show = self.phase in ("spin", "result")
        st = {
            "game": self.game,
            "table": self.name,
            "table_id": self.id,
            "phase": self.phase,
            "remaining": round(self.remaining(), 2),
            "phase_total": self.phase_total,
            "max_players": self.max_players,
            "round_no": self.round_no,
            "players": [
                {"seat": q.seat, "name": q.name, "bet": self.bets_total(q),
                 "win": q.last_win if self.phase == "result" else 0,
                 "you": q.pid == p.pid}
                for q in sorted(self.players.values(), key=lambda x: x.seat)
            ],
            "history": list(self.history),
            "outcome": self.outcome if show else None,
            "you": {
                "seat": p.seat,
                "chips": p.chips,
                "safe_chips": self.refundable(p),
                "bets": dict(p.bets),
                "total_bet": self.bets_total(p),
                "win": p.last_win if self.phase == "result" else 0,
                "can_rebet": bool(p.last_bets),
            },
        }
        st.update(self.extra_state())
        return st


# ================================ РУЛЕТКА ====================================

ROULETTE_ORDER = [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23,
                  10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26]
RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def roulette_mult(kind, value, n):
    """Сколько раз ставка возвращается (вместе с самой ставкой). 0 — проигрыш."""
    if kind == "num":
        return 36 if n == value else 0
    if n == 0:
        return 0
    if kind == "red":
        return 2 if n in RED else 0
    if kind == "black":
        return 2 if n not in RED else 0
    if kind == "even":
        return 2 if n % 2 == 0 else 0
    if kind == "odd":
        return 2 if n % 2 == 1 else 0
    if kind == "low":
        return 2 if 1 <= n <= 18 else 0
    if kind == "high":
        return 2 if 19 <= n <= 36 else 0
    if kind == "dozen":
        return 3 if (n - 1) // 12 + 1 == value else 0
    if kind == "col":
        return 3 if (n - 1) % 3 + 1 == value else 0
    return 0


class RouletteTable(BetTable):
    game = "roulette"
    max_players = 10
    soft_cap = 6
    BETTING_SEC = 20
    SPIN_SEC = 9
    RESULT_SEC = 6

    def valid_bet(self, kind, value):
        if kind == "num":
            return 0 <= value <= 36
        if kind in ("dozen", "col"):
            return 1 <= value <= 3
        return kind in ("red", "black", "even", "odd", "low", "high")

    def draw(self):
        return secrets.randbelow(37)

    def settle(self, p):
        total = 0
        for key, amt in p.bets.items():
            kind, v = key.split(":")
            total += amt * roulette_mult(kind, int(v), self.outcome)
        return total

    def extra_state(self):
        return {"wheel": ROULETTE_ORDER}


# ============================== ТРИ ТОПОРА ===================================
# Доска 4 колонки x 3 ряда = 12 клеток (индекс = ряд*4 + колонка).
# За раунд три топора втыкаются в 3 РАЗНЫЕ клетки. Коэффициент каждой ставки
# считается из точной вероятности (перебор всех 220 комбинаций), RTP ~ 94%.

AX_RTP = 0.94
AX_TESTS = {}
for _i in range(12):
    AX_TESTS[("cell", _i)] = (lambda s, i=_i: i in s)
for _r in range(3):
    AX_TESTS[("row", _r)] = (lambda s, r=_r: any(x // 4 == r for x in s))
    AX_TESTS[("row3", _r)] = (lambda s, r=_r: all(x // 4 == r for x in s))
for _c in range(4):
    AX_TESTS[("col", _c)] = (lambda s, c=_c: any(x % 4 == c for x in s))
    AX_TESTS[("col3", _c)] = (lambda s, c=_c: all(x % 4 == c for x in s))

_ALL = list(combinations(range(12), 3))
AX_MULT = {}
for _k, _f in AX_TESTS.items():
    _cnt = sum(1 for _s in _ALL if _f(set(_s)))
    AX_MULT[_k] = math.floor(AX_RTP * len(_ALL) / _cnt * 100) / 100
AX_MULT_JSON = {f"{k[0]}:{k[1]}": v for k, v in AX_MULT.items()}


class AxesTable(BetTable):
    game = "axes"
    max_players = 8
    soft_cap = 5
    BETTING_SEC = 20
    SPIN_SEC = 6
    RESULT_SEC = 5

    def valid_bet(self, kind, value):
        return (kind, value) in AX_TESTS

    def draw(self):
        return RNG.sample(range(12), 3)   # порядок = порядок бросков

    def settle(self, p):
        s = set(self.outcome)
        total = 0
        for key, amt in p.bets.items():
            kind, v = key.split(":")
            k = (kind, int(v))
            if AX_TESTS[k](s):
                total += int(amt * AX_MULT[k])
        return total

    def history_item(self):
        return sorted(self.outcome)

    def extra_state(self):
        return {"mults": AX_MULT_JSON}


# ================================ БЛЭКДЖЕК ===================================

RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
SUITS = ["S", "H", "D", "C"]


def new_shoe(decks=6):
    shoe = [(r, s) for _ in range(decks) for r in RANKS for s in SUITS]
    RNG.shuffle(shoe)
    return shoe


def hand_value(cards):
    total, aces = 0, 0
    for r, _ in cards:
        if r == "A":
            total += 11
            aces += 1
        elif r in ("J", "Q", "K"):
            total += 10
        else:
            total += int(r)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0


def is_bj(cards):
    return len(cards) == 2 and hand_value(cards)[0] == 21


class BlackjackTable(BaseTable):
    game = "blackjack"
    max_players = 5
    soft_cap = 3
    BET_SEC = 15
    TURN_SEC = 25
    PAYOUT_SEC = 7
    DEALER_STEP = 0.9
    MAX_BET = 50000

    def __init__(self, num):
        super().__init__(num)
        self.shoe = new_shoe()
        self.dealer = []
        self.hole_hidden = True
        self.turn = None
        self.turn_deadline = 0.0
        self.next_dealer_at = 0.0
        self.round_no = 0
        self.fast = False
        self.set_phase("betting", self.BET_SEC)

    def draw_card(self):
        if not self.shoe:
            self.shoe = new_shoe()
        return self.shoe.pop()

    def refundable(self, p):
        return p.chips + p.bet if self.phase == "betting" else p.chips

    def at_stake(self, p):
        if self.phase == "betting" or (self.phase in ("playing", "dealer") and p.in_round):
            return p.bet
        return 0

    def reset_idle(self):
        self.dealer = []
        self.hole_hidden = True
        self.turn = None
        self.fast = False
        for p in self.players.values():
            p.cards, p.bet, p.status, p.result, p.in_round, p.last_win = [], 0, "idle", None, False, 0
        self.set_phase("betting", self.BET_SEC)

    def remove_player(self, pid):
        p = self.players.get(pid)
        if not p:
            return None
        if self.phase == "betting":
            p.chips += p.bet
            p.bet = 0
        was_turn = self.turn == pid
        del self.players[pid]
        if not self.players:
            self.reset_idle()
        elif was_turn and self.phase == "playing":
            self.advance_turn(time.time())
        return p.chips

    # ---------- сообщения игрока ----------
    def on_message(self, p, mtype, msg):
        if mtype == "casino_action":
            return self.act(p, msg.get("action"))
        if mtype not in ("casino_bet", "casino_clear", "casino_rebet"):
            return False
        if self.phase != "betting":
            self.err(p, "Ставки сейчас не принимаются")
            return False
        if mtype == "casino_clear":
            if p.bet:
                p.chips += p.bet
                p.bet = 0
                return True
            return False
        if mtype == "casino_rebet":
            if p.last_bet <= 0:
                return False
            p.chips += p.bet
            p.bet = min(p.last_bet, p.chips)
            p.chips -= p.bet
            return True
        amount = clamp_int(msg.get("amount"), 0, self.MAX_BET)
        if amount <= 0:
            return False
        if amount > p.chips:
            self.err(p, "Недостаточно фишек")
            return False
        if p.bet + amount > self.MAX_BET:
            self.err(p, "Достигнут максимум ставки")
            return False
        p.chips -= amount
        p.bet += amount
        return True

    def act(self, p, action):
        if self.phase != "playing" or self.turn != p.pid:
            self.err(p, "Сейчас не ваш ход")
            return False
        t = time.time()
        if action == "hit":
            p.cards.append(self.draw_card())
            tot, _ = hand_value(p.cards)
            if tot > 21:
                p.status = "bust"
            elif tot == 21:
                p.status = "stand"
        elif action == "stand":
            p.status = "stand"
        elif action == "double":
            if len(p.cards) != 2 or p.chips < p.bet:
                self.err(p, "Удвоение недоступно")
                return False
            p.chips -= p.bet
            p.bet *= 2
            p.cards.append(self.draw_card())
            p.status = "bust" if hand_value(p.cards)[0] > 21 else "stand"
        else:
            return False
        if p.status == "waiting":
            self.turn_deadline = t + self.TURN_SEC
        else:
            self.advance_turn(t)
        return True

    # ---------- ход раунда ----------
    def tick(self, t):
        if self.phase == "betting":
            if not self.players:
                self.deadline = t + self.BET_SEC
                self.phase_total = float(self.BET_SEC)
                self.fast = False
                return False
            if not self.fast and all(q.bet > 0 for q in self.players.values()):
                self.fast = True
                if self.deadline - t > 3:
                    self.deadline = t + 3
                    return True
            if t >= self.deadline:
                active = [q for q in self.players.values() if q.bet > 0]
                if not active:
                    self.set_phase("betting", self.BET_SEC, t)
                    self.fast = False
                    return True
                self.start_deal(t, active)
                return True
            return False
        if self.phase == "playing":
            if self.turn and t >= self.turn_deadline:
                p = self.players.get(self.turn)
                if p:
                    p.status = "stand"
                self.advance_turn(t)
                return True
            return False
        if self.phase == "dealer":
            if t >= self.next_dealer_at:
                self.dealer_step(t)
                return True
            return False
        if self.phase == "payout":
            if t >= self.deadline:
                self.start_betting(t)
                return True
        return False

    def start_deal(self, t, active):
        if len(self.shoe) < 78:
            self.shoe = new_shoe()
        self.round_no += 1
        for p in self.players.values():
            p.cards = []
            p.result = None
            p.last_win = 0
            p.in_round = p.bet > 0
            p.status = "waiting" if p.in_round else "idle"
            if p.in_round:
                p.last_bet = p.bet
        self.dealer = []
        self.hole_hidden = True
        order = sorted(active, key=lambda x: x.seat)
        for _ in range(2):
            for p in order:
                p.cards.append(self.draw_card())
            self.dealer.append(self.draw_card())
        self.set_phase("playing", 0, t)
        for p in order:
            if is_bj(p.cards):
                p.status = "blackjack"
        if self.dealer[0][0] in ("A", "10", "J", "Q", "K") and is_bj(self.dealer):
            self.hole_hidden = False      # дилер с блэкджеком — раунд сразу заканчивается
            self.finish_round(t)
            return
        self.advance_turn(t)

    def advance_turn(self, t):
        for q in sorted(self.players.values(), key=lambda x: x.seat):
            if q.in_round and q.status == "waiting":
                self.turn = q.pid
                self.turn_deadline = t + self.TURN_SEC
                return
        self.turn = None
        self.start_dealer(t)

    def start_dealer(self, t):
        self.set_phase("dealer", 0, t)
        self.hole_hidden = False
        self.next_dealer_at = t + 1.0

    def dealer_step(self, t):
        live = any(q.in_round and q.status == "stand" for q in self.players.values())
        total, _ = hand_value(self.dealer)
        if live and total < 17:
            self.dealer.append(self.draw_card())
            self.next_dealer_at = t + self.DEALER_STEP
        else:
            self.finish_round(t)

    def finish_round(self, t):
        d_total, _ = hand_value(self.dealer)
        d_bj = is_bj(self.dealer)
        for p in self.players.values():
            if not p.in_round:
                continue
            tot, _ = hand_value(p.cards)
            if p.status == "bust":
                res, pay = "bust", 0
            elif p.status == "blackjack":
                if d_bj:
                    res, pay = "push", p.bet
                else:
                    res, pay = "blackjack", p.bet + (p.bet * 3) // 2
            elif d_bj:
                res, pay = "lose", 0
            elif d_total > 21 or tot > d_total:
                res, pay = "win", p.bet * 2
            elif tot == d_total:
                res, pay = "push", p.bet
            else:
                res, pay = "lose", 0
            p.chips += pay
            p.last_win = pay - p.bet
            p.result = res
        self.hole_hidden = False
        self.turn = None
        self.set_phase("payout", self.PAYOUT_SEC, t)

    def start_betting(self, t):
        self.dealer = []
        self.hole_hidden = True
        self.turn = None
        self.fast = False
        for p in self.players.values():
            p.cards, p.bet, p.status, p.result, p.in_round, p.last_win = [], 0, "idle", None, False, 0
        self.set_phase("betting", self.BET_SEC, t)

    def state_for(self, p):
        d = self.dealer
        if self.hole_hidden and len(d) >= 2:
            shown = [list(d[0]), None]
            dt = hand_value([d[0]])[0]
        else:
            shown = [list(c) for c in d]
            dt = hand_value(d)[0] if d else 0
        plist = []
        for q in sorted(self.players.values(), key=lambda x: x.seat):
            tot, soft = hand_value(q.cards) if q.cards else (0, False)
            plist.append({
                "seat": q.seat, "name": q.name, "bet": q.bet,
                "cards": [list(c) for c in q.cards], "total": tot, "soft": soft,
                "status": q.status, "result": q.result, "win": q.last_win,
                "in_round": q.in_round, "you": q.pid == p.pid,
            })
        actions = []
        if self.phase == "playing" and self.turn == p.pid:
            actions = ["hit", "stand"]
            if len(p.cards) == 2 and p.chips >= p.bet:
                actions.append("double")
        turn_p = self.players.get(self.turn) if self.turn else None
        return {
            "game": self.game, "table": self.name, "table_id": self.id,
            "phase": self.phase,
            "remaining": round(self.remaining(), 2), "phase_total": self.phase_total,
            "max_players": self.max_players, "round_no": self.round_no,
            "dealer": {"cards": shown, "total": dt},
            "players": plist,
            "turn": turn_p.seat if turn_p else None,
            "turn_remaining": round(max(0.0, self.turn_deadline - time.time()), 2) if turn_p else 0,
            "turn_total": self.TURN_SEC,
            "actions": actions,
            "you": {
                "seat": p.seat, "chips": p.chips, "safe_chips": self.refundable(p),
                "bet": p.bet, "can_rebet": p.last_bet > 0,
            },
        }



# ============================ ИГРОВОЙ АВТОМАТ =================================
# Тянете ручку — выпадают 3 символа. Три одинаковых платят по ценности символа
# (чем реже символ, тем больше множитель), два одинаковых — меньший приз.

SLOT_SYMS = ["cherry", "lemon", "bell", "star", "diamond", "seven"]
SLOT_WEIGHTS = [30, 25, 20, 13, 8, 4]
SLOT_X3 = {"cherry": 5, "lemon": 8, "bell": 15, "star": 30, "diamond": 80, "seven": 300}
SLOT_X2 = {"cherry": 0.5, "lemon": 0.8, "bell": 1, "star": 1.5, "diamond": 2, "seven": 5}
SLOT_MAX_BET = 10000


def slot_payout(reels, bet):
    if reels[0] == reels[1] == reels[2]:
        return int(bet * SLOT_X3[reels[0]])
    for s in set(reels):
        if reels.count(s) == 2:
            return int(bet * SLOT_X2[s])
    return 0


def slot_rtp():
    tot = float(sum(SLOT_WEIGHTS))
    p = {k: w / tot for k, w in zip(SLOT_SYMS, SLOT_WEIGHTS)}
    return sum(p[k] ** 3 * SLOT_X3[k] + 3 * p[k] ** 2 * (1 - p[k]) * SLOT_X2[k] for k in SLOT_SYMS)


class SlotsTable(BaseTable):
    """Личный автомат: за каждым игроком свой «стол» из одного места."""
    game = "slots"
    max_players = 1
    soft_cap = 1
    SPIN_SEC = 2.8

    def __init__(self, num):
        super().__init__(num)
        self.reels = None
        self.bet = 0
        self.pending = 0
        self.last_win = 0
        self.round_no = 0
        self.history = []
        self.set_phase("idle", 0)

    def refundable(self, p):
        return p.chips + (self.pending if self.phase == "spin" else 0)

    def at_stake(self, p):
        return self.pending if self.phase == "spin" else 0

    def on_message(self, p, mtype, msg):
        if mtype != "casino_spin":
            return False
        if self.phase != "idle":
            return False
        amount = clamp_int(msg.get("amount"), 0, SLOT_MAX_BET)
        if amount <= 0:
            return False
        if amount > p.chips:
            self.err(p, "Недостаточно фишек")
            return False
        p.chips -= amount
        self.bet = amount
        self.reels = RNG.choices(SLOT_SYMS, weights=SLOT_WEIGHTS, k=3)
        self.pending = slot_payout(self.reels, amount)
        self.last_win = 0
        self.round_no += 1
        self.set_phase("spin", self.SPIN_SEC)
        return True

    def tick(self, t):
        if self.phase == "spin" and t >= self.deadline:
            for p in self.players.values():
                p.chips += self.pending
            self.last_win = self.pending - self.bet
            self.history.append({"reels": self.reels, "bet": self.bet, "win": self.pending})
            self.history = self.history[-8:]
            self.pending = 0
            self.set_phase("idle", 0, t)
            return True
        return False

    def state_for(self, p):
        return {
            "game": self.game, "table": self.name, "table_id": self.id,
            "phase": self.phase, "remaining": round(self.remaining(), 2),
            "phase_total": self.phase_total, "round_no": self.round_no,
            "max_players": self.max_players, "players": [],
            "reels": self.reels, "bet": self.bet,
            "win": self.last_win if self.phase == "idle" else 0,
            "history": list(self.history),
            "paytable": {"syms": SLOT_SYMS, "x3": SLOT_X3, "x2": SLOT_X2},
            "max_bet": SLOT_MAX_BET,
            "you": {"seat": p.seat, "chips": p.chips, "safe_chips": self.refundable(p)},
        }


# ============================ менеджер столов ================================

class Casino:
    GAMES = {
        "roulette": RouletteTable,
        "axes": AxesTable,
        "blackjack": BlackjackTable,
        "slots": SlotsTable,
    }

    def __init__(self):
        self.lock = threading.RLock()
        self.tables = {g: {} for g in self.GAMES}
        self.counters = {g: 0 for g in self.GAMES}
        self.where = {}        # pid -> (game, table_id)
        self.running = False
        for g in self.GAMES:
            self._new_table(g)

    # ---- столы ----
    def _new_table(self, game):
        self.counters[game] += 1
        t = self.GAMES[game](self.counters[game])
        self.tables[game][t.id] = t
        return t

    def pick_table(self, game):
        """Равномерное заполнение: игрок садится за стол с минимумом игроков.
        Когда все столы набрали soft_cap человек, открывается новый стол —
        и новички идут к нему, пока столы не выровняются."""
        free = [t for t in self.tables[game].values() if len(t.players) < t.max_players]
        if not free or all(len(t.players) >= t.soft_cap for t in free):
            return self._new_table(game)
        return min(free, key=lambda t: (len(t.players), t.num))

    def _cleanup(self, game):
        tables = self.tables[game]
        empty = [t for t in tables.values() if not t.players]
        # оставляем минимум один стол на игру
        while len(tables) > 1 and empty:
            t = empty.pop()
            del tables[t.id]

    # ---- жизненный цикл ----
    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            time.sleep(TICK)
            t = time.time()
            with self.lock:
                for tables in self.tables.values():
                    for table in list(tables.values()):
                        try:
                            if table.tick(t):
                                table.broadcast()
                        except Exception:
                            traceback.print_exc()

    def at_stake(self):
        """{имя_в_нижнем_регистре: фишки на кону} по всем столам казино."""
        out = {}
        with self.lock:
            for tables in self.tables.values():
                for t in tables.values():
                    for p in t.players.values():
                        v = t.at_stake(p)
                        if v:
                            k = p.name.lower()
                            out[k] = out.get(k, 0) + v
        return out

    def stats(self):
        with self.lock:
            return {g: {"players": sum(len(t.players) for t in ts.values()),
                        "tables": len(ts)} for g, ts in self.tables.items()}

    # ---- сообщения ----
    def handle(self, h, msg):
        mtype = msg.get("type")
        with self.lock:
            if mtype == "casino_stats":
                safe_send(h, {"type": "casino_stats", "stats": self.stats()})
                return
            if mtype == "casino_join":
                self._join(h, msg)
                return
            if mtype == "casino_leave":
                self.leave(h, notify=True)
                return
            loc = self.where.get(h.pid)
            if not loc:
                return
            table = self.tables[loc[0]].get(loc[1])
            p = table.players.get(h.pid) if table else None
            if not p:
                return
            try:
                changed = table.on_message(p, mtype, msg)
            except Exception:
                traceback.print_exc()
                changed = False
            if changed:
                table.broadcast()

    def _join(self, h, msg):
        game = msg.get("game")
        if game not in self.GAMES:
            safe_send(h, {"type": "casino_error", "message": "Неизвестная игра"})
            return
        if h.pid in self.where:
            self.leave(h, notify=False)
        acc = getattr(h, "account", None)
        if acc is None:
            safe_send(h, {"type": "auth_required"})
            return
        table = self.pick_table(game)
        p = table.add_player(h.pid, acc, h)
        if p is None:
            safe_send(h, {"type": "casino_error", "message": "Нет свободных мест"})
            return
        self.where[h.pid] = (game, table.id)
        safe_send(h, {"type": "casino_joined", "game": game,
                      "table": table.name, "table_id": table.id})
        table.broadcast()

    def leave(self, h, notify=False):
        with self.lock:
            loc = self.where.pop(h.pid, None)
            if not loc:
                if notify:
                    safe_send(h, {"type": "casino_left", "chips": None})
                return
            game, tid = loc
            table = self.tables[game].get(tid)
            chips = table.remove_player(h.pid) if table else None
            acc = getattr(h, "account", None)
            if acc:
                acc.push()
            if notify:
                safe_send(h, {"type": "casino_left", "chips": chips})
            if table:
                table.broadcast()
            self._cleanup(game)
