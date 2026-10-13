// -*- coding: utf-8 -*-
// Клиент покера: работает одинаково в браузере на ПК и на телефоне.
// Банк фишек, имя и ежедневный бонус хранятся на сервере в аккаунте игрока.

const SUIT_RED = new Set(["♥", "♦"]);

// Расположение мест вокруг овального стола в процентах (left, top).
// Индекс 0 — всегда "вы" (внизу по центру), остальные распределены по кругу.
const SEAT_LAYOUTS = {
  1: [{ left: 50, top: 88 }],
  2: [{ left: 50, top: 88 }, { left: 50, top: 12 }],
  3: [{ left: 50, top: 88 }, { left: 15, top: 22 }, { left: 85, top: 22 }],
  4: [{ left: 50, top: 90 }, { left: 8, top: 50 }, { left: 50, top: 10 }, { left: 92, top: 50 }],
  5: [
    { left: 50, top: 90 }, { left: 6, top: 60 }, { left: 22, top: 15 },
    { left: 78, top: 15 }, { left: 94, top: 60 },
  ],
};

const state = {
  name: "Игрок",
  bank: 0,
  bonusLeft: 0,          // секунд до следующего ежедневного бонуса
  bonusAmount: 1000,
  session: null,         // постоянное соединение: банк, бонус
  ws: null,              // соединение со столом
  connectedToTable: false,
  roomCode: null,
  lastState: null,
  myLastKnownChips: null,
  pendingBuyIn: null,    // { type: 'create'|'join'|'server'|'random', ... }
  pendingConnect: false,
  rebuyModalOpen: false,
  turnTimerHandle: null,
};

const el = (id) => document.getElementById(id);

function showScreen(id) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.add("hidden"));
  el(id).classList.remove("hidden");
  // Дневной бонус виден только в меню, бейдж кода стола — только за столом
  el("daily-bonus").classList.toggle("hidden", id !== "screen-menu");
  el("room-code-badge").classList.toggle("hidden", id !== "screen-table" || !state.roomCode);
}

// ---------------- Аккаунт и ежедневный бонус (всё считает сервер) ----------------

function onAccount(a) {
  state.name = a.username;
  state.bank = a.bank;
  state.bonusLeft = a.bonus_left;
  state.bonusAmount = a.bonus_amount;
  el("player-name").textContent = a.username;
  refreshBankDisplay();
  updateBonusButton();
}

function updateBonusButton() {
  const btn = el("daily-bonus");
  if (state.bonusLeft <= 0) {
    btn.disabled = false;
    btn.textContent = `🎁 Получить ${state.bonusAmount}`;
  } else {
    btn.disabled = true;
    const left = Math.ceil(state.bonusLeft);
    const h = Math.floor(left / 3600);
    const m = Math.floor((left % 3600) / 60);
    const s = left % 60;
    const pad = (n) => String(n).padStart(2, "0");
    btn.textContent = `Бонус через ${pad(h)}:${pad(m)}:${pad(s)}`;
  }
}

function claimBonus() {
  if (state.bonusLeft > 0) return;
  if (state.session) state.session.send({ type: "claim_bonus" });
}

function refreshBankDisplay() {
  const bankEl = el("bank-chips");
  if (bankEl) bankEl.textContent = state.bank;
}

// ---------------- Меню ----------------

function initMenu() {
  if (!Account.requireLogin()) return;
  state.session = Account.session({
    onAccount,
    onMessage: (msg) => {
      if (msg.type === "bonus_result" && !msg.ok) alert("Бонус ещё не доступен.");
    },
  });
  refreshBankDisplay();

  el("daily-bonus").addEventListener("click", claimBonus);
  el("help-btn").addEventListener("click", () => el("help-modal").classList.remove("hidden"));
  el("help-close").addEventListener("click", () => el("help-modal").classList.add("hidden"));
  el("btn-logout").addEventListener("click", () => Account.logout());

  const needChips = () => {
    if (state.bank <= 0) {
      alert("В банке нет фишек. Заберите ежедневный бонус (кнопка «🎁» в углу).");
      return false;
    }
    return true;
  };

  el("btn-server").addEventListener("click", () => { if (needChips()) openBuyInModal({ type: "server" }); });
  el("btn-random").addEventListener("click", () => { if (needChips()) openBuyInModal({ type: "random" }); });
  el("btn-create").addEventListener("click", () => { if (needChips()) openTableSetupModal(); });
  el("btn-join").addEventListener("click", () => el("join-box").classList.remove("hidden"));
  el("btn-join-cancel").addEventListener("click", () => el("join-box").classList.add("hidden"));
  el("btn-join-confirm").addEventListener("click", () => {
    const code = el("input-code").value.trim().toUpperCase();
    if (!code) return;
    if (!needChips()) return;
    openBuyInModal({ type: "join", code });
  });

  el("btn-leave-table").addEventListener("click", leaveTable);

  el("act-fold").addEventListener("click", () => sendMsg({ type: "action", action: "fold" }));
  el("act-check").addEventListener("click", () => sendMsg({ type: "action", action: "check" }));
  el("act-call").addEventListener("click", () => sendMsg({ type: "action", action: "call" }));
  el("act-raise").addEventListener("click", () =>
    sendMsg({ type: "action", action: "raise", amount: parseInt(el("raise-amount").value || "0", 10) })
  );
  el("act-allin").addEventListener("click", () => sendMsg({ type: "action", action: "allin" }));

  el("buyin-confirm").addEventListener("click", confirmBuyIn);
  el("buyin-cancel").addEventListener("click", cancelBuyIn);
  el("buyin-slider").addEventListener("input", () => {
    el("buyin-amount-display").textContent = el("buyin-slider").value;
  });
  el("rebuy-confirm").addEventListener("click", confirmRebuy);
  el("rebuy-leave").addEventListener("click", () => {
    hideRebuyModal();
    leaveTable();
  });

  el("setup-confirm").addEventListener("click", confirmTableSetup);
  el("setup-cancel").addEventListener("click", () => el("table-setup-modal").classList.add("hidden"));

  updateBonusButton();
  setInterval(() => {
    if (state.bonusLeft > 0) state.bonusLeft -= 1;
    updateBonusButton();
  }, 1000);
  registerServiceWorker();
}

// ---------------- Настройки стола (только при создании) ----------------

function openTableSetupModal() {
  el("setup-min-buyin").value = 0;
  el("setup-max-buyin").value = 0;
  el("setup-small-blind").value = 10;
  el("setup-big-blind").value = 20;
  el("setup-private").checked = true;
  el("table-setup-modal").classList.remove("hidden");
}

function confirmTableSetup() {
  const minBuyin = Math.max(0, parseInt(el("setup-min-buyin").value || "0", 10));
  const maxBuyinRaw = Math.max(0, parseInt(el("setup-max-buyin").value || "0", 10));
  const smallBlind = Math.max(1, parseInt(el("setup-small-blind").value || "10", 10));
  const bigBlind = Math.max(2, parseInt(el("setup-big-blind").value || "20", 10));
  if (bigBlind <= smallBlind) {
    alert("Максимальный блайнд (BB) должен быть больше минимального (SB).");
    return;
  }
  if (maxBuyinRaw && maxBuyinRaw < minBuyin) {
    alert("Максимальный вход не может быть меньше минимального.");
    return;
  }
  if (minBuyin > state.bank) {
    alert(`В банке ${state.bank} фишек — недостаточно для минимального входа ${minBuyin}, который вы указали.`);
    return;
  }

  el("table-setup-modal").classList.add("hidden");
  openBuyInModal({
    type: "create",
    min_buyin: minBuyin,
    max_buyin: maxBuyinRaw,
    small_blind: smallBlind,
    big_blind: bigBlind,
    private: el("setup-private").checked,
  });
}

// ---------------- Бай-ин (сколько фишек занести за стол, ползунком) ----------------

function openBuyInModal(pending) {
  state.pendingBuyIn = pending;
  el("buyin-bank-max").textContent = state.bank;

  // Серверный стол требует вход от 400 (20 больших блайндов); у случайного стола
  // минимум зависит от того, какой стол выпадет — сервер подскажет, если не хватит.
  const minAmount = pending.type === "create" ? Math.max(1, pending.min_buyin || 0)
    : pending.type === "server" ? Math.min(400, state.bank) : 1;
  let maxAmount = state.bank;
  if (pending.type === "create" && pending.max_buyin) {
    maxAmount = Math.min(pending.max_buyin, state.bank);
  }
  if (maxAmount < minAmount) maxAmount = minAmount;

  const slider = el("buyin-slider");
  slider.min = minAmount;
  slider.max = Math.max(minAmount, maxAmount);
  slider.step = Math.max(1, Math.round((slider.max - minAmount) / 100) || 1);
  slider.value = Math.min(slider.max, Math.max(minAmount, Math.min(state.bank, 1000)));
  el("buyin-amount-display").textContent = slider.value;
  el("buyin-range-label").textContent = `от ${minAmount} до ${slider.max}`;

  el("buyin-modal").classList.remove("hidden");
}

function cancelBuyIn() {
  state.pendingBuyIn = null;
  el("buyin-modal").classList.add("hidden");
}

function confirmBuyIn() {
  const amount = parseInt(el("buyin-slider").value || "0", 10);
  const pending = state.pendingBuyIn;
  const minAmount = parseInt(el("buyin-slider").min, 10);
  const maxAmount = parseInt(el("buyin-slider").max, 10);
  if (!amount || amount < minAmount || amount > maxAmount || amount > state.bank) {
    alert(`Введите сумму от ${minAmount} до ${Math.min(maxAmount, state.bank)}`);
    return;
  }
  // Фишки спишет сервер при успешной посадке за стол — банк обновится сам.
  state.pendingConnect = true;
  el("buyin-modal").classList.add("hidden");
  el("join-box").classList.add("hidden");

  state.connectedToTable = true;
  showScreen("screen-table");
  el("stage-label").textContent = "Подключение...";

  if (pending.type === "create") {
    connect(() => sendMsg({
      type: "create_room",
      chips: amount,
      min_buyin: pending.min_buyin || 0,
      max_buyin: pending.max_buyin || 0,
      small_blind: pending.small_blind || 10,
      big_blind: pending.big_blind || 20,
      private: pending.private !== false,
    }));
  } else if (pending.type === "server") {
    connect(() => sendMsg({ type: "join_server", chips: amount }));
  } else if (pending.type === "random") {
    connect(() => sendMsg({ type: "join_random", chips: amount }));
  } else {
    connect(() => sendMsg({ type: "join_room", code: pending.code, chips: amount }));
  }
  state.pendingBuyIn = null;
}

// ---------------- Докупка фишек (ребай) ----------------
// Показывается ТОЛЬКО когда у вас нет карт на руках (т.е. раунд, в котором
// вы участвовали, точно завершился) — иначе окно всплывало бы прямо в момент
// ва-банка, ещё до того, как стал понятен исход раздачи.

function showRebuyModal() {
  if (state.rebuyModalOpen) return;
  state.rebuyModalOpen = true;
  if (state.bank <= 0) {
    alert("Фишки за столом и в банке закончились. Заберите ежедневный бонус — возвращаемся в меню.");
    state.rebuyModalOpen = false;
    leaveTable();
    return;
  }
  el("rebuy-bank-max").textContent = state.bank;
  const amountInput = el("rebuy-amount");
  amountInput.max = state.bank;
  amountInput.value = Math.max(1, Math.min(state.bank, 500));
  el("rebuy-modal").classList.remove("hidden");
}

function hideRebuyModal() {
  state.rebuyModalOpen = false;
  el("rebuy-modal").classList.add("hidden");
}

function confirmRebuy() {
  const amount = parseInt(el("rebuy-amount").value || "0", 10);
  if (!amount || amount < 1 || amount > state.bank) {
    alert(`Введите сумму от 1 до ${state.bank}`);
    return;
  }
  sendMsg({ type: "rebuy", amount });   // сервер спишет фишки с банка
  hideRebuyModal();
}

function maybeShowRebuy(me) {
  if (!me) return;
  if (me.chips > 0) {
    if (state.rebuyModalOpen) hideRebuyModal();
    return;
  }
  // Пока у вас на руках есть карты — вы всё ещё участвуете в текущей
  // раздаче (например, только что пошли ва-банк). Не мешаем её досмотреть.
  const stillInHand = me.hole && me.hole.length > 0;
  if (stillInHand) return;
  if (state.rebuyModalOpen) return;
  showRebuyModal();
}

// ---------------- Подключение к столу ----------------

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

function connect(onOpen) {
  const ws = new WebSocket(wsUrl());
  state.ws = ws;
  ws.addEventListener("open", () => {
    ws.send(JSON.stringify({ type: "auth_token", token: Account.token() }));
    onOpen && onOpen();
  });
  ws.addEventListener("message", (ev) => {
    try {
      handleMsg(JSON.parse(ev.data));
    } catch (e) {
      console.error("Bad message", e);
    }
  });
  ws.addEventListener("close", () => {
    if (state.connectedToTable) {
      returnToMenu();
      alert("Связь со столом прервана.");
    }
  });
  ws.addEventListener("error", () => {
    alert("Не удалось подключиться к серверу.");
  });
}

function sendMsg(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(obj));
  }
}

// Остаток стека сервер сам вернёт в банк при выходе из-за стола.
function leaveTable() {
  sendMsg({ type: "leave" });
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
  returnToMenu();
}

function returnToMenu() {
  state.connectedToTable = false;
  state.pendingConnect = false;
  state.lastState = null;
  state.myLastKnownChips = null;
  state.roomCode = null;
  hideRebuyModal();
  stopTurnTimer();
  refreshBankDisplay();
  showScreen("screen-menu");
  if (state.session) state.session.send({ type: "account_get" });
}

// ---------------- Обработка сообщений сервера ----------------

function handleMsg(msg) {
  if (msg.type === "room_created" || msg.type === "joined") {
    state.roomCode = msg.code;
    state.pendingConnect = false;
    const label = msg.code.startsWith("SRV") ? "Серверный стол" : `Код: ${msg.code}`;
    el("room-code-badge").textContent = label;
    el("room-code-badge").classList.remove("hidden");
  } else if (msg.type === "auth_required" || msg.type === "auth_error") {
    Account.setToken(null);
    Account.toLogin();
  } else if (msg.type === "error") {
    alert(msg.message);
    if (!state.lastState && state.pendingConnect) {
      // Не удалось сесть за стол (не хватает до минимального входа и т. п.) —
      // сервер ничего не списывал, просто возвращаемся в меню.
      state.pendingConnect = false;
      if (state.ws) { state.ws.close(); state.ws = null; }
      returnToMenu();
    }
  } else if (msg.type === "state") {
    renderState(msg.state);
  }
}

function renderState(s) {
  state.lastState = s;
  const me = s.players.find((p) => p.is_you);
  if (me) state.myLastKnownChips = me.chips;

  showScreen("screen-table");

  const waiting = s.stage === "waiting";
  const stageNames = { preflop: "Префлоп", flop: "Флоп", turn: "Тёрн", river: "Ривер", showdown: "Вскрытие" };

  if (waiting) {
    const n = s.players.length;
    el("pot-label").textContent = `Блайнды: ${s.small_blind}/${s.big_blind}` +
      (s.min_buyin ? ` · Мин. вход: ${s.min_buyin}` : "") +
      (s.max_buyin ? ` · Макс: ${s.max_buyin}` : "");
    el("stage-label").textContent =
      n >= 2
        ? `Игроков за столом: ${n}/5 — раздача начнётся автоматически через пару секунд`
        : `Игроков за столом: ${n}/5 — нужно минимум 2, чтобы начать`;
  } else {
    el("pot-label").textContent = `Банк: ${s.pot}`;
    let stageText = `Этап: ${stageNames[s.stage] || s.stage} · Раздача №${s.hand_number}`;
    if (s.stage === "showdown" && s.last_result) {
      stageText += ` · Победитель: ${s.last_result.winners.join(", ")} (+${s.last_result.pot})`;
    }
    el("stage-label").textContent = stageText;
  }

  const communityBox = el("community-cards");
  communityBox.innerHTML = "";
  s.community.forEach((c) => communityBox.appendChild(makeCard(c)));

  renderPlayerSeats(s.players, s);

  const holeBox = el("hole-cards");
  holeBox.innerHTML = "";
  if (me && me.hole && me.hole.length) {
    const label = document.createElement("div");
    label.style.alignSelf = "center";
    label.style.marginRight = "6px";
    label.style.color = "#a5d6a7";
    label.textContent = "Ваши карты:";
    holeBox.appendChild(label);
    me.hole.forEach((c) => holeBox.appendChild(makeCard(c)));
  } else if (!waiting && me) {
    const note = document.createElement("div");
    note.style.color = "#a5d6a7";
    note.style.fontSize = "0.85em";
    note.textContent = "Вы вступите в игру со следующей раздачи.";
    holeBox.appendChild(note);
  }

  el("log-box").textContent = (s.log || []).join("\n");
  el("log-box").scrollTop = el("log-box").scrollHeight;

  const actions = s.your_actions || [];
  el("act-fold").disabled = !actions.includes("fold");
  el("act-check").disabled = !actions.includes("check");
  el("act-call").disabled = !actions.includes("call");
  el("act-call").textContent = actions.includes("call") ? `Колл (${s.your_to_call})` : "Колл";
  el("act-raise").disabled = !actions.includes("raise");
  el("act-allin").disabled = !actions.includes("allin");

  updateTurnTimer(s);
  maybeShowRebuy(me);
}

// ---------------- Таймер на ход ----------------

function stopTurnTimer() {
  if (state.turnTimerHandle) {
    clearInterval(state.turnTimerHandle);
    state.turnTimerHandle = null;
  }
  el("turn-timer").textContent = "";
  el("turn-timer").classList.remove("urgent");
}

function updateTurnTimer(s) {
  stopTurnTimer();
  if (!s.turn_deadline || !s.current_turn) return;

  const turnPlayer = s.players.find((p) => p.id === s.current_turn);
  const name = turnPlayer ? (turnPlayer.is_you ? "Вы" : turnPlayer.name) : "";
  const box = el("turn-timer");

  const tick = () => {
    const remaining = Math.max(0, Math.ceil(s.turn_deadline - Date.now() / 1000));
    box.textContent = `Ход: ${name} — ${remaining} сек`;
    box.classList.toggle("urgent", remaining <= 5);
    if (remaining <= 0) {
      clearInterval(state.turnTimerHandle);
      state.turnTimerHandle = null;
    }
  };
  tick();
  state.turnTimerHandle = setInterval(tick, 500);
}

// ---------------- Отрисовка круглого стола ----------------

function rotateToSelf(players) {
  const idx = players.findIndex((p) => p.is_you);
  if (idx < 0) return players;
  return players.slice(idx).concat(players.slice(0, idx));
}

function clearSeats() {
  document.querySelectorAll(".player-seat").forEach((e) => e.remove());
}

function renderPlayerSeats(players, s) {
  clearSeats();
  const rotated = rotateToSelf(players);
  const layout = SEAT_LAYOUTS[rotated.length] || SEAT_LAYOUTS[5];
  const oval = el("poker-oval");
  rotated.forEach((p, i) => {
    const pos = layout[i] || layout[layout.length - 1];
    const seat = renderPlayerSeatBox(p, s);
    seat.style.left = pos.left + "%";
    seat.style.top = pos.top + "%";
    oval.appendChild(seat);
  });
}

function renderPlayerSeatBox(p, s) {
  const box = document.createElement("div");
  const isShowdown = s && s.stage === "showdown" && s.last_result;
  const isWinner = isShowdown && s.last_result.winners.includes(p.name);
  box.className = "player-seat" + (p.is_turn ? " turn" : "") + (isWinner ? " winner" : "");

  const name = document.createElement("div");
  name.className = "player-name";
  name.textContent = p.name + (p.is_you ? " (вы)" : "");
  box.appendChild(name);

  const chips = document.createElement("div");
  chips.className = "player-chips";
  chips.textContent = `${p.chips} фишек`;
  box.appendChild(chips);

  const statuses = [];
  if (p.folded) statuses.push("Фолд");
  if (p.all_in) statuses.push("Ва-банк");
  if (!p.connected) statuses.push("Отключён");
  if (statuses.length) {
    const st = document.createElement("div");
    st.className = "player-status";
    st.textContent = statuses.join(" / ");
    box.appendChild(st);
  }

  if (p.current_bet) {
    const bet = document.createElement("div");
    bet.className = "player-bet";
    bet.textContent = `Ставка: ${p.current_bet}`;
    box.appendChild(bet);
  }

  const cardsRow = document.createElement("div");
  cardsRow.className = "player-cards";
  (p.hole || []).forEach((c) => cardsRow.appendChild(makeCard(c)));
  box.appendChild(cardsRow);

  if (isShowdown && s.last_result.hands && s.last_result.hands[p.name]) {
    const combo = document.createElement("div");
    combo.className = "player-combo";
    combo.textContent = s.last_result.hands[p.name].score_desc;
    box.appendChild(combo);
  }

  return box;
}

function makeCard(cardStr) {
  const d = document.createElement("div");
  if (cardStr === "??") {
    d.className = "card back";
    d.textContent = "🂠";
    return d;
  }
  const suit = cardStr.slice(-1);
  const rank = cardStr.slice(0, -1);
  d.className = "card" + (SUIT_RED.has(suit) ? " red" : "");
  d.textContent = rank + suit;
  return d;
}

function registerServiceWorker() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
}

document.addEventListener("DOMContentLoaded", initMenu);

// ---------------- Скрытая админ-панель (только для вас) ----------------
// Открывается 5 быстрыми тапами по заголовку в главном меню. Доступ
// проверяется НА СЕРВЕРЕ по IP-адресу (переменная POKER_ADMIN_IPS) —
// без пароля, только по разрешённым адресам.

let titleTapCount = 0;
let titleTapTimer = null;

function ensureAdminConnection(callback) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    callback(state.ws, false);
    return;
  }
  const tempWs = new WebSocket(wsUrl());
  tempWs.addEventListener("open", () => callback(tempWs, true));
  tempWs.addEventListener("error", () => alert("Не удалось подключиться к серверу для проверки."));
}

function setupAdminTrigger() {
  const title = el("app-title");
  if (!title) return;
  title.addEventListener("click", () => {
    titleTapCount += 1;
    clearTimeout(titleTapTimer);
    titleTapTimer = setTimeout(() => { titleTapCount = 0; }, 2500);
    if (titleTapCount >= 5) {
      titleTapCount = 0;
      requestAdminAccess();
    }
  });

  el("admin-panel-close").addEventListener("click", () => el("admin-panel-modal").classList.add("hidden"));
  el("admin-set-bank").addEventListener("click", () => {
    const amount = parseInt(el("admin-bank-amount").value || "0", 10);
    if (isNaN(amount) || amount < 0) return;
    if (state.session) state.session.send({ type: "admin_set_bank", amount });
  });
  el("admin-set-table").addEventListener("click", () => {
    const amount = parseInt(el("admin-table-amount").value || "0", 10);
    if (isNaN(amount) || amount < 0) return;
    sendMsg({ type: "admin_set_chips", amount });
  });
}

function requestAdminAccess() {
  ensureAdminConnection((sock, isTemp) => {
    const onMessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type !== "admin_verify_result") return;
      sock.removeEventListener("message", onMessage);
      if (isTemp) sock.close();
      if (!msg.ok) {
        alert(`Доступ запрещён. Ваш IP: ${msg.your_ip}. Добавьте его в POKER_ADMIN_IPS на сервере, чтобы открыть панель отсюда.`);
        return;
      }
      openAdminPanel();
    };
    sock.addEventListener("message", onMessage);
    sock.send(JSON.stringify({ type: "admin_verify" }));
  });
}

function openAdminPanel() {
  el("admin-bank-amount").value = state.bank;
  el("admin-table-section").classList.toggle("hidden", !state.connectedToTable);
  if (state.connectedToTable && state.myLastKnownChips !== null) {
    el("admin-table-amount").value = state.myLastKnownChips;
  }
  el("admin-panel-modal").classList.remove("hidden");
}

document.addEventListener("DOMContentLoaded", setupAdminTrigger);
