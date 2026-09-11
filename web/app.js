// -*- coding: utf-8 -*-
// Клиент покера: работает одинаково в браузере на ПК и на телефоне.

const SUIT_RED = new Set(["♥", "♦"]);
const BONUS_AMOUNT = 1000;
const BONUS_COOLDOWN_MS = 24 * 60 * 60 * 1000;

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
  name: localStorage.getItem("poker_name") || "Игрок",
  bank: localStorage.getItem("poker_bank") !== null
    ? parseInt(localStorage.getItem("poker_bank"), 10)
    : 1000,
  lastBonus: localStorage.getItem("poker_last_bonus")
    ? parseInt(localStorage.getItem("poker_last_bonus"), 10)
    : null,
  ws: null,
  isHost: false,
  lastState: null,
  myLastKnownChips: null,
  pendingBuyIn: null,   // { type: 'create' } или { type: 'join', code }
  rebuyModalOpen: false,
};

const el = (id) => document.getElementById(id);

function showScreen(id) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.add("hidden"));
  el(id).classList.remove("hidden");
}

function saveName() { localStorage.setItem("poker_name", state.name); }
function saveBank() { localStorage.setItem("poker_bank", String(state.bank)); }
function saveBonus() { localStorage.setItem("poker_last_bonus", String(state.lastBonus)); }

// ---------------- Ежедневный бонус ----------------

function updateBonusButton() {
  const btn = el("daily-bonus");
  if (state.lastBonus === null) {
    btn.disabled = false;
    btn.textContent = "🎁 Получить 1000";
    return;
  }
  const elapsed = Date.now() - state.lastBonus;
  const remaining = BONUS_COOLDOWN_MS - elapsed;
  if (remaining <= 0) {
    btn.disabled = false;
    btn.textContent = "🎁 Получить 1000";
  } else {
    btn.disabled = true;
    const h = Math.floor(remaining / 3600000);
    const m = Math.floor((remaining % 3600000) / 60000);
    const s = Math.floor((remaining % 60000) / 1000);
    const pad = (n) => String(n).padStart(2, "0");
    btn.textContent = `Бонус через ${pad(h)}:${pad(m)}:${pad(s)}`;
  }
}

function claimBonus() {
  if (state.lastBonus !== null && Date.now() - state.lastBonus < BONUS_COOLDOWN_MS) return;
  state.bank += BONUS_AMOUNT;
  state.lastBonus = Date.now();
  saveBank();
  saveBonus();
  refreshBankDisplay();
  updateBonusButton();
}

function refreshBankDisplay() {
  const bankEl = el("bank-chips");
  if (bankEl) bankEl.textContent = state.bank;
}

// ---------------- Меню ----------------

function initMenu() {
  el("input-name").value = state.name;
  refreshBankDisplay();

  el("input-name").addEventListener("change", () => {
    const v = el("input-name").value.trim();
    state.name = v || "Игрок";
    saveName();
  });

  el("daily-bonus").addEventListener("click", claimBonus);
  el("help-btn").addEventListener("click", () => el("help-modal").classList.remove("hidden"));
  el("help-close").addEventListener("click", () => el("help-modal").classList.add("hidden"));

  el("btn-create").addEventListener("click", () => {
    if (state.bank <= 0) {
      alert("В банке нет фишек. Дождитесь дневного бонуса (кнопка «🎁 Бонус» в углу).");
      return;
    }
    openBuyInModal({ type: "create" });
  });
  el("btn-join").addEventListener("click", () => el("join-box").classList.remove("hidden"));
  el("btn-join-cancel").addEventListener("click", () => el("join-box").classList.add("hidden"));
  el("btn-join-confirm").addEventListener("click", () => {
    const code = el("input-code").value.trim().toUpperCase();
    if (!code) return;
    if (state.bank <= 0) {
      alert("В банке нет фишек. Дождитесь дневного бонуса (кнопка «🎁 Бонус» в углу).");
      return;
    }
    openBuyInModal({ type: "join", code });
  });

  el("btn-start-hand").addEventListener("click", () => sendMsg({ type: "start_hand" }));
  el("btn-leave-waiting").addEventListener("click", leaveTable);
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
  el("rebuy-confirm").addEventListener("click", confirmRebuy);
  el("rebuy-leave").addEventListener("click", () => {
    hideRebuyModal();
    leaveTable();
  });

  updateBonusButton();
  setInterval(updateBonusButton, 1000);
  registerServiceWorker();
}

// ---------------- Бай-ин (сколько фишек занести за стол) ----------------

function openBuyInModal(pending) {
  state.pendingBuyIn = pending;
  el("buyin-title").textContent =
    pending.type === "create" ? "Сколько фишек занести за стол?" : "Сколько фишек занести за стол?";
  el("buyin-bank-max").textContent = state.bank;
  const amountInput = el("buyin-amount");
  amountInput.max = state.bank;
  amountInput.value = Math.max(1, Math.min(state.bank, 1000));
  el("buyin-modal").classList.remove("hidden");
}

function cancelBuyIn() {
  state.pendingBuyIn = null;
  el("buyin-modal").classList.add("hidden");
}

function confirmBuyIn() {
  const amount = parseInt(el("buyin-amount").value || "0", 10);
  if (!amount || amount < 1 || amount > state.bank) {
    alert(`Введите сумму от 1 до ${state.bank}`);
    return;
  }
  const pending = state.pendingBuyIn;
  state.bank -= amount;
  saveBank();
  refreshBankDisplay();
  el("buyin-modal").classList.add("hidden");

  if (pending.type === "create") {
    state.isHost = true;
    showScreen("screen-waiting");
    el("room-code-display").textContent = "Подключение...";
    el("btn-start-hand").classList.add("hidden");
    connect(() => sendMsg({ type: "create_room", name: state.name, chips: amount }));
  } else {
    state.isHost = false;
    el("join-box").classList.add("hidden");
    showScreen("screen-waiting");
    el("room-code-display").textContent = "Подключение...";
    el("btn-start-hand").classList.add("hidden");
    connect(() => sendMsg({ type: "join_room", code: pending.code, name: state.name, chips: amount }));
  }
  state.pendingBuyIn = null;
}

// ---------------- Докупка фишек (ребай) ----------------

function showRebuyModal() {
  if (state.rebuyModalOpen) return;
  state.rebuyModalOpen = true;
  if (state.bank <= 0) {
    alert("Фишки за столом и в банке закончились. Дождитесь дневного бонуса — возвращаемся в меню.");
    state.rebuyModalOpen = false;
    leaveTable();
    return;
  }
  el("rebuy-with-bank").classList.remove("hidden");
  el("rebuy-no-bank").classList.add("hidden");
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
  state.bank -= amount;
  saveBank();
  refreshBankDisplay();
  sendMsg({ type: "rebuy", amount });
  hideRebuyModal();
}

// ---------------- Подключение ----------------

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

function connect(onOpen) {
  const ws = new WebSocket(wsUrl());
  state.ws = ws;
  ws.addEventListener("open", () => onOpen && onOpen());
  ws.addEventListener("message", (ev) => {
    try {
      handleMsg(JSON.parse(ev.data));
    } catch (e) {
      console.error("Bad message", e);
    }
  });
  ws.addEventListener("close", () => {
    if (state.lastState !== null || state.isHost) {
      returnToMenu(true);
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

function leaveTable() {
  if (state.myLastKnownChips !== null) {
    state.bank += state.myLastKnownChips;
    saveBank();
  }
  sendMsg({ type: "leave" });
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
  returnToMenu(false);
}

function returnToMenu(alreadyCredited) {
  if (!alreadyCredited && state.myLastKnownChips !== null) {
    state.bank += state.myLastKnownChips;
    saveBank();
  }
  state.lastState = null;
  state.myLastKnownChips = null;
  hideRebuyModal();
  refreshBankDisplay();
  showScreen("screen-menu");
}

// ---------------- Обработка сообщений сервера ----------------

function handleMsg(msg) {
  if (msg.type === "room_created") {
    el("room-code-display").textContent = msg.code;
    el("btn-start-hand").classList.remove("hidden");
  } else if (msg.type === "joined") {
    el("room-code-display").textContent = msg.code;
  } else if (msg.type === "error") {
    alert(msg.message);
  } else if (msg.type === "state") {
    renderState(msg.state);
  }
}

function renderState(s) {
  state.lastState = s;
  const me = s.players.find((p) => p.is_you);
  if (me) state.myLastKnownChips = me.chips;

  if (s.stage === "waiting") {
    showScreen("screen-waiting");
    const n = s.players.length;
    el("waiting-info").textContent =
      n >= 2
        ? `Игроков за столом: ${n}/5. Раздача начнётся автоматически через пару секунд, либо нажмите кнопку.`
        : `Игроков за столом: ${n}/5. Нужно минимум 2 игрока.`;
    const box = el("waiting-players");
    box.innerHTML = "";
    s.players.forEach((p) => {
      const d = document.createElement("div");
      d.textContent = `${p.name}${p.is_you ? " (вы)" : ""} — ${p.chips} фишек`;
      box.appendChild(d);
    });
    maybeShowRebuy(me, s);
    return;
  }

  showScreen("screen-table");

  el("pot-label").textContent = `Банк: ${s.pot}`;
  const stageNames = { preflop: "Префлоп", flop: "Флоп", turn: "Тёрн", river: "Ривер", showdown: "Вскрытие" };
  let stageText = `Этап: ${stageNames[s.stage] || s.stage} · Раздача №${s.hand_number}`;
  if (s.stage === "showdown" && s.last_result) {
    stageText += ` · Победитель: ${s.last_result.winners.join(", ")} (+${s.last_result.pot})`;
  }
  el("stage-label").textContent = stageText;

  const communityBox = el("community-cards");
  communityBox.innerHTML = "";
  s.community.forEach((c) => communityBox.appendChild(makeCard(c)));

  renderPlayerSeats(s.players);

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

  maybeShowRebuy(me, s);
}

function maybeShowRebuy(me, s) {
  if (!me) return;
  if (me.chips > 0) {
    if (state.rebuyModalOpen) hideRebuyModal();
    return;
  }
  if (state.rebuyModalOpen) return;
  if (s.stage === "waiting" || s.stage === "showdown") {
    showRebuyModal();
  }
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

function renderPlayerSeats(players) {
  clearSeats();
  const rotated = rotateToSelf(players);
  const layout = SEAT_LAYOUTS[rotated.length] || SEAT_LAYOUTS[5];
  const oval = el("poker-oval");
  rotated.forEach((p, i) => {
    const pos = layout[i] || layout[layout.length - 1];
    const seat = renderPlayerSeatBox(p);
    seat.style.left = pos.left + "%";
    seat.style.top = pos.top + "%";
    oval.appendChild(seat);
  });
}

function renderPlayerSeatBox(p) {
  const box = document.createElement("div");
  box.className = "player-seat" + (p.is_turn ? " turn" : "");

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
