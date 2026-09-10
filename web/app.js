// -*- coding: utf-8 -*-
// Клиент покера: работает одинаково в браузере на ПК и на телефоне.

const SUIT_RED = new Set(["♥", "♦"]);

const state = {
  name: localStorage.getItem("poker_name") || "Игрок",
  chips: parseInt(localStorage.getItem("poker_chips") || "1000", 10),
  ws: null,
  isHost: false,
  lastState: null,
  myLastKnownChips: null,
};

const el = (id) => document.getElementById(id);

function showScreen(id) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.add("hidden"));
  el(id).classList.remove("hidden");
}

function saveWallet() {
  localStorage.setItem("poker_name", state.name);
  localStorage.setItem("poker_chips", String(state.chips));
}

// ---------------- Меню ----------------

function initMenu() {
  el("input-name").value = state.name;
  el("wallet-chips").textContent = state.chips;

  el("input-name").addEventListener("change", () => {
    const v = el("input-name").value.trim();
    state.name = v || "Игрок";
    saveWallet();
  });

  el("btn-create").addEventListener("click", createTable);
  el("btn-join").addEventListener("click", () => el("join-box").classList.remove("hidden"));
  el("btn-join-cancel").addEventListener("click", () => el("join-box").classList.add("hidden"));
  el("btn-join-confirm").addEventListener("click", joinTable);

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

  if (location.protocol === "file:") {
    el("connection-hint").textContent =
      "Открывайте страницу через адрес сервера (http://...), а не как локальный файл.";
  }

  ensureWalletNotEmpty();
  registerServiceWorker();
}

function ensureWalletNotEmpty() {
  if (state.chips <= 0) {
    state.chips = 1000;
    saveWallet();
    el("wallet-chips").textContent = state.chips;
  }
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
      alert("Связь со столом прервана.");
      returnToMenu(true);
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

function createTable() {
  ensureWalletNotEmpty();
  state.isHost = true;
  showScreen("screen-waiting");
  el("room-code-display").textContent = "Подключение...";
  el("btn-start-hand").classList.add("hidden");
  connect(() => {
    sendMsg({ type: "create_room", name: state.name, chips: state.chips });
  });
}

function joinTable() {
  ensureWalletNotEmpty();
  const code = el("input-code").value.trim().toUpperCase();
  if (!code) return;
  state.isHost = false;
  showScreen("screen-waiting");
  el("room-code-display").textContent = "Подключение...";
  el("btn-start-hand").classList.add("hidden");
  connect(() => {
    sendMsg({ type: "join_room", code, name: state.name, chips: state.chips });
  });
}

function leaveTable() {
  if (state.myLastKnownChips !== null) {
    state.chips = state.myLastKnownChips;
    saveWallet();
  }
  sendMsg({ type: "leave" });
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
  returnToMenu(false);
}

function returnToMenu(keepChipsFromLastKnown) {
  if (keepChipsFromLastKnown && state.myLastKnownChips !== null) {
    state.chips = state.myLastKnownChips;
    saveWallet();
  }
  state.lastState = null;
  state.myLastKnownChips = null;
  el("wallet-chips").textContent = state.chips;
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

  const playersRow = el("players-row");
  playersRow.innerHTML = "";
  s.players.forEach((p) => playersRow.appendChild(renderPlayerBox(p)));

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
}

function renderPlayerBox(p) {
  const box = document.createElement("div");
  box.className = "player-box" + (p.is_turn ? " turn" : "");

  const name = document.createElement("div");
  name.className = "player-name";
  name.textContent = p.name + (p.is_you ? " (вы)" : "");
  box.appendChild(name);

  const chips = document.createElement("div");
  chips.className = "player-chips";
  chips.textContent = `Фишки: ${p.chips}`;
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
