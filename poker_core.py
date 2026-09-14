# -*- coding: utf-8 -*-
"""
Ядро игры: колода карт, оценка покерных комбинаций,
состояние стола и логика раундов торговли (Техасский холдем).
"""

import random
import itertools
import time
from collections import Counter

RANKS = list(range(2, 15))  # 2..14 (14 = туз)
SUITS = ["♠", "♥", "♦", "♣"]

RANK_NAMES = {
    11: "J", 12: "Q", 13: "K", 14: "A",
    **{i: str(i) for i in range(2, 11)}
}

HAND_NAMES = {
    9: "Роял-флеш",
    8: "Стрит-флеш",
    7: "Каре",
    6: "Фулл-хаус",
    5: "Флеш",
    4: "Стрит",
    3: "Сет (тройка)",
    2: "Две пары",
    1: "Пара",
    0: "Старшая карта",
}


def new_deck():
    deck = [(r, s) for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def card_str(card):
    r, s = card
    return f"{RANK_NAMES[r]}{s}"


def _check_straight(unique_ranks_desc):
    """unique_ranks_desc: отсортированный по убыванию список уникальных рангов."""
    ranks = unique_ranks_desc[:]
    if 14 in ranks:
        ranks = ranks + [1]  # туз может быть младшим для стрита A-2-3-4-5
    for i in range(len(ranks) - 4):
        window = ranks[i:i + 5]
        if window[0] - window[4] == 4 and len(set(window)) == 5:
            return True, window[0]
    return False, None


def eval_5(cards):
    """Оценка ровно 5 карт. Возвращает кортеж для сравнения (больше = сильнее)."""
    ranks = sorted([c[0] for c in cards], reverse=True)
    suits = [c[1] for c in cards]
    is_flush = len(set(suits)) == 1
    unique_ranks = sorted(set(ranks), reverse=True)
    is_straight, straight_high = _check_straight(unique_ranks)

    counts = Counter(ranks)
    # (количество, ранг) отсортировано по убыванию количества, затем ранга
    grouped = sorted(counts.items(), key=lambda x: (-x[1], -x[0]))

    if is_straight and is_flush:
        return (8, straight_high)
    if grouped[0][1] == 4:
        four = grouped[0][0]
        kicker = max(r for r in ranks if r != four)
        return (7, four, kicker)
    if grouped[0][1] == 3 and grouped[1][1] >= 2:
        return (6, grouped[0][0], grouped[1][0])
    if is_flush:
        return (5,) + tuple(ranks)
    if is_straight:
        return (4, straight_high)
    if grouped[0][1] == 3:
        three = grouped[0][0]
        kickers = sorted([r for r in ranks if r != three], reverse=True)[:2]
        return (3, three) + tuple(kickers)
    if grouped[0][1] == 2 and grouped[1][1] == 2:
        pair_hi, pair_lo = sorted([grouped[0][0], grouped[1][0]], reverse=True)
        kicker = max(r for r in ranks if r != pair_hi and r != pair_lo)
        return (2, pair_hi, pair_lo, kicker)
    if grouped[0][1] == 2:
        pair = grouped[0][0]
        kickers = sorted([r for r in ranks if r != pair], reverse=True)[:3]
        return (1, pair) + tuple(kickers)
    return (0,) + tuple(ranks)


def best_hand(seven_cards):
    """Лучшая комбинация из 7 карт (2 карманные + 5 общих)."""
    best = None
    for combo in itertools.combinations(seven_cards, 5):
        score = eval_5(list(combo))
        if best is None or score > best:
            best = score
    return best


def hand_description(score):
    return HAND_NAMES.get(score[0], "?")


# ---------------------- Состояние стола ----------------------

class Player:
    def __init__(self, pid, name, chips):
        self.id = pid
        self.name = name
        self.chips = chips
        self.hole = []
        self.folded = False
        self.all_in = False
        self.current_bet = 0   # ставка в текущем раунде торговли
        self.total_bet_hand = 0
        self.connected = True
        self.sitting_out = False  # ждёт следующей раздачи (только что зашёл)

    def to_public_dict(self, reveal=False):
        return {
            "id": self.id,
            "name": self.name,
            "chips": self.chips,
            "folded": self.folded,
            "all_in": self.all_in,
            "current_bet": self.current_bet,
            "connected": self.connected,
            "hole": [card_str(c) for c in self.hole] if reveal else (["??", "??"] if self.hole else []),
        }


class Table:
    """Логика одного покерного стола (максимум 5 игроков)."""

    MAX_PLAYERS = 5
    SMALL_BLIND = 10
    BIG_BLIND = 20
    TURN_SECONDS = 20  # сколько секунд даётся на ход, потом автодействие

    def __init__(self, code, host_id, small_blind=None, big_blind=None, min_buyin=0, max_buyin=0):
        self.code = code
        self.host_id = host_id
        if small_blind and big_blind and big_blind > small_blind:
            self.SMALL_BLIND = int(small_blind)
            self.BIG_BLIND = int(big_blind)
        self.min_buyin = max(0, int(min_buyin or 0))
        self.max_buyin = max(0, int(max_buyin or 0))
        if self.max_buyin and self.min_buyin and self.max_buyin < self.min_buyin:
            self.max_buyin = self.min_buyin
        self.players = {}          # id -> Player
        self.seat_order = []       # список id в порядке посадки
        self.dealer_pos = -1
        self.deck = []
        self.community = []
        self.pot = 0
        self.stage = "waiting"     # waiting, preflop, flop, turn, river, showdown
        self.current_turn = None   # id игрока, чей ход
        self.min_raise = self.BIG_BLIND
        self.highest_bet = 0
        self.last_aggressor = None
        self.acted_this_round = set()
        self.log = []
        self.hand_number = 0
        self.turn_token = 0        # увеличивается при каждой смене хода (для отмены старых таймеров)
        self.turn_deadline = None  # unix-время, к которому нужно походить

    # ---------- управление игроками ----------

    def add_player(self, pid, name, chips):
        if len(self.players) >= self.MAX_PLAYERS:
            return False, "Стол уже заполнен (максимум 5 игроков)"
        if self.min_buyin and chips < self.min_buyin:
            return False, f"Минимальный вход за этот стол: {self.min_buyin} фишек"
        if self.max_buyin and chips > self.max_buyin:
            return False, f"Максимальный вход за этот стол: {self.max_buyin} фишек"
        if any(p.name == name for p in self.players.values()):
            name = name + "_2"
        p = Player(pid, name, chips)
        if self.stage != "waiting":
            p.sitting_out = True  # подключился в середине раздачи - ждёт
        self.players[pid] = p
        self.seat_order.append(pid)
        self.log.append(f"{name} присоединился к столу")
        return True, "ok"

    def remove_player(self, pid):
        if pid in self.players:
            name = self.players[pid].name
            self.log.append(f"{name} покинул стол")
            if pid in self.seat_order:
                self.seat_order.remove(pid)
            del self.players[pid]

    def active_players(self):
        """Игроки, сидящие за столом (не sitting_out), с фишками."""
        return [self.players[pid] for pid in self.seat_order
                if pid in self.players and not self.players[pid].sitting_out and self.players[pid].chips > 0]

    # ---------- раздача ----------

    def can_start_hand(self):
        return len([p for p in self.active_players()]) >= 2

    def start_hand(self):
        self.hand_number += 1
        self.deck = new_deck()
        self.community = []
        self.pot = 0
        self.stage = "preflop"
        self.highest_bet = 0
        self.acted_this_round = set()
        self.log = [f"--- Раздача #{self.hand_number} ---"]

        for p in self.players.values():
            p.hole = []
            p.folded = False
            p.all_in = False
            p.current_bet = 0
            p.total_bet_hand = 0
            if p.chips <= 0:
                p.sitting_out = True
            elif p.sitting_out and p.chips > 0:
                p.sitting_out = False  # заходит в новую раздачу

        players = self.active_players()
        n = len(players)
        self.dealer_pos = (self.dealer_pos + 1) % n

        # раздать карманные карты
        for p in players:
            p.hole = [self.deck.pop(), self.deck.pop()]

        sb_idx = (self.dealer_pos + 1) % n if n > 2 else (self.dealer_pos + 1) % n
        bb_idx = (self.dealer_pos + 2) % n if n > 2 else self.dealer_pos

        if n == 2:
            # хедз-ап: дилер = малый блайнд
            sb_idx = self.dealer_pos
            bb_idx = (self.dealer_pos + 1) % n

        sb_player = players[sb_idx]
        bb_player = players[bb_idx]
        self._post_bet(sb_player, self.SMALL_BLIND)
        self._post_bet(bb_player, self.BIG_BLIND)
        self.log.append(f"{sb_player.name} ставит малый блайнд {self.SMALL_BLIND}")
        self.log.append(f"{bb_player.name} ставит большой блайнд {self.BIG_BLIND}")

        self.highest_bet = self.BIG_BLIND
        self.min_raise = self.BIG_BLIND

        first_idx = (bb_idx + 1) % n
        self.current_turn = players[first_idx].id
        self._skip_to_actionable()

    def _post_bet(self, p, amount):
        amount = min(amount, p.chips)
        p.chips -= amount
        p.current_bet += amount
        p.total_bet_hand += amount
        self.pot += amount
        if p.chips == 0:
            p.all_in = True

    # ---------- торговля ----------

    def _players_in_hand(self):
        return [p for p in self.active_players() if not p.folded]

    def _next_seat(self, pid):
        order = [p.id for p in self.active_players()]
        if pid not in order:
            return order[0] if order else None
        i = order.index(pid)
        return order[(i + 1) % len(order)]

    def _skip_to_actionable(self):
        """Пропускает игроков, которые сфолдили или в олл-ине, ищет следующего живого."""
        start = self.current_turn
        for _ in range(self.MAX_PLAYERS + 1):
            p = self.players.get(self.current_turn)
            if p and not p.folded and not p.all_in and p.chips > 0:
                break
            self.current_turn = self._next_seat(self.current_turn)
            if self.current_turn == start:
                break
        self._touch_turn()

    def _touch_turn(self):
        """Обновляет "токен" хода и дедлайн на действие. Вызывается при каждой смене хода —
        старый запланированный таймер автовыхода (на сервере) станет недействительным."""
        self.turn_token += 1
        if self.current_turn and self.stage in ("preflop", "flop", "turn", "river"):
            self.turn_deadline = time.time() + self.TURN_SECONDS
        else:
            self.turn_deadline = None

    def legal_actions(self, pid):
        p = self.players.get(pid)
        if not p or self.current_turn != pid or p.folded or p.all_in:
            return []
        actions = ["fold"]
        to_call = self.highest_bet - p.current_bet
        if to_call <= 0:
            actions.append("check")
        else:
            actions.append("call")
        if p.chips > to_call:
            actions.append("raise")
        actions.append("allin")
        return actions

    def apply_action(self, pid, action, amount=0):
        p = self.players.get(pid)
        if not p or self.current_turn != pid:
            return False, "Сейчас не ваш ход"
        if p.folded or p.all_in:
            return False, "Вы не можете сейчас действовать"

        to_call = self.highest_bet - p.current_bet

        if action == "fold":
            p.folded = True
            self.log.append(f"{p.name} сбрасывает карты (фолд)")
        elif action == "check":
            if to_call > 0:
                return False, "Нельзя чекнуть, есть неуравненная ставка"
            self.log.append(f"{p.name} чек")
        elif action == "call":
            call_amt = min(to_call, p.chips)
            self._post_bet(p, call_amt)
            self.log.append(f"{p.name} уравнивает ({call_amt})")
        elif action == "raise":
            amount = int(amount)
            total_needed = to_call + max(amount, self.min_raise)
            total_needed = min(total_needed, p.chips)
            if total_needed <= to_call:
                return False, "Недостаточно фишек для рейза"
            self._post_bet(p, total_needed)
            self.min_raise = max(self.min_raise, p.current_bet - self.highest_bet)
            self.highest_bet = p.current_bet
            self.last_aggressor = pid
            self.acted_this_round = {pid}
            self.log.append(f"{p.name} повышает до {p.current_bet}")
        elif action == "allin":
            allin_amt = p.chips
            self._post_bet(p, allin_amt)
            if p.current_bet > self.highest_bet:
                self.min_raise = max(self.min_raise, p.current_bet - self.highest_bet)
                self.highest_bet = p.current_bet
                self.last_aggressor = pid
                self.acted_this_round = {pid}
            self.log.append(f"{p.name} идёт ва-банк ({allin_amt})")
        else:
            return False, "Неизвестное действие"

        self.acted_this_round.add(pid)
        self._advance_turn()
        return True, "ok"

    def _advance_turn(self):
        if len(self._players_in_hand()) <= 1:
            self._finish_hand_by_fold()
            return

        nxt = self._next_seat(self.current_turn)
        self.current_turn = nxt
        self._skip_to_actionable()

        if self._betting_round_complete():
            self._next_stage()

    def _betting_round_complete(self):
        in_hand = [p for p in self._players_in_hand() if not p.all_in and p.chips > 0]
        if not in_hand:
            return True
        for p in in_hand:
            if p.current_bet != self.highest_bet:
                return False
            if p.id not in self.acted_this_round:
                return False
        return True

    def _next_stage(self):
        for p in self.players.values():
            p.current_bet = 0
        self.highest_bet = 0
        self.min_raise = self.BIG_BLIND
        self.acted_this_round = set()

        if self.stage == "preflop":
            self.community += [self.deck.pop() for _ in range(3)]
            self.stage = "flop"
            self.log.append("=== Флоп: " + ", ".join(card_str(c) for c in self.community) + " ===")
        elif self.stage == "flop":
            self.community.append(self.deck.pop())
            self.stage = "turn"
            self.log.append("=== Тёрн: " + card_str(self.community[-1]) + " ===")
        elif self.stage == "turn":
            self.community.append(self.deck.pop())
            self.stage = "river"
            self.log.append("=== Ривер: " + card_str(self.community[-1]) + " ===")
        elif self.stage == "river":
            self._showdown()
            return

        # первый ход после дилера
        players = self.active_players()
        order = [p.id for p in players]
        d = order[self.dealer_pos % len(order)] if order else None
        self.current_turn = self._next_seat(d) if d else (order[0] if order else None)
        self._skip_to_actionable()

        remaining = [p for p in self._players_in_hand() if not p.all_in and p.chips > 0]
        if len(remaining) <= 1:
            # все ва-банк - докручиваем карты без торговли
            self._auto_run_out()

    def _auto_run_out(self):
        while self.stage != "river":
            if self.stage == "preflop":
                self.community += [self.deck.pop() for _ in range(3)]
                self.stage = "flop"
            elif self.stage == "flop":
                self.community.append(self.deck.pop())
                self.stage = "turn"
            elif self.stage == "turn":
                self.community.append(self.deck.pop())
                self.stage = "river"
        self._showdown()

    def _finish_hand_by_fold(self):
        winner = self._players_in_hand()[0] if self._players_in_hand() else None
        if winner:
            winner.chips += self.pot
            self.log.append(f"{winner.name} забирает банк {self.pot} (все остальные сбросили)")
        self.stage = "showdown"
        self.current_turn = None
        self._touch_turn()
        self.last_result = {
            "winners": [winner.name] if winner else [],
            "pot": self.pot,
            "showdown": False,
            "hands": {},
        }
        self.pot = 0

    def _showdown(self):
        self.stage = "showdown"
        self.current_turn = None
        self._touch_turn()
        contenders = self._players_in_hand()
        results = {}
        best_score = None
        winners = []
        for p in contenders:
            score = best_hand(p.hole + self.community)
            results[p.name] = {
                "score_desc": hand_description(score),
                "cards": [card_str(c) for c in p.hole],
            }
            if best_score is None or score > best_score:
                best_score = score
                winners = [p]
            elif score == best_score:
                winners.append(p)

        share = self.pot // len(winners) if winners else 0
        remainder = self.pot - share * len(winners) if winners else 0
        for i, w in enumerate(winners):
            w.chips += share + (remainder if i == 0 else 0)

        self.log.append("=== Вскрытие карт ===")
        for p in contenders:
            self.log.append(f"{p.name}: {' '.join(card_str(c) for c in p.hole)} — {results[p.name]['score_desc']}")
        self.log.append(f"Победитель(и): {', '.join(w.name for w in winners)} — выигрыш {self.pot}")

        self.last_result = {
            "winners": [w.name for w in winners],
            "pot": self.pot,
            "showdown": True,
            "hands": results,
        }
        self.pot = 0

    # ---------- сериализация состояния для клиента ----------

    def public_state(self, for_pid=None):
        show_all = self.stage == "showdown"
        players = []
        for pid in self.seat_order:
            p = self.players.get(pid)
            if not p:
                continue
            reveal = show_all or (pid == for_pid)
            d = p.to_public_dict(reveal=reveal)
            d["is_you"] = (pid == for_pid)
            d["is_turn"] = (pid == self.current_turn)
            players.append(d)

        return {
            "code": self.code,
            "stage": self.stage,
            "community": [card_str(c) for c in self.community],
            "pot": self.pot,
            "players": players,
            "current_turn": self.current_turn,
            "highest_bet": self.highest_bet,
            "min_raise": self.min_raise,
            "dealer_pos": self.dealer_pos,
            "log": self.log[-12:],
            "your_actions": self.legal_actions(for_pid) if for_pid else [],
            "your_to_call": max(0, self.highest_bet - (self.players[for_pid].current_bet if for_pid in self.players else 0)),
            "hand_number": self.hand_number,
            "last_result": getattr(self, "last_result", None),
            "turn_deadline": self.turn_deadline,
            "turn_seconds": self.TURN_SECONDS,
            "small_blind": self.SMALL_BLIND,
            "big_blind": self.BIG_BLIND,
            "min_buyin": self.min_buyin,
            "max_buyin": self.max_buyin,
        }
