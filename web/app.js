// -*- coding: utf-8 -*-
// Клиент покера: работает одинаково в браузере на ПК и на телефоне.

const SUIT_RED = new Set(["♥", "♦"]);
const BONUS_AMOUNT = 1000;
const BONUS_COOLDOWN_MS = 24 * 60 * 60 * 1000;
const BG_PRICE = 20000;
const CUSTOM_BG_PRICE = 100000;

// Пресеты фонов для магазина. Сам стол (зелёный овал) цвет не меняет —
// меняется только фон вокруг него, чтобы карты не терялись на фоне стола.
const BG_PRESETS = [
  { id: "bg_navy", name: "Тёмно-синий", color: "#0d1b3e" },
  { id: "bg_purple", name: "Баклажан", color: "#2e1a47" },
  { id: "bg_wine", name: "Бордовый", color: "#4a1620" },
  { id: "bg_charcoal", name: "Графит", color: "#232323" },
  { id: "bg_petrol", name: "Тёмная бирюза", color: "#0b3d3a" },
  { id: "bg_brown", name: "Кофейный", color: "#3b2a1a" },
  { id: "bg_indigo", name: "Индиго", color: "#1a1a4e" },
  { id: "bg_black", name: "Чёрный", color: "#0a0a0a" },
];
const DEFAULT_BG = { id: "default", name: "Обычный", color: "#0b3d24" };

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

function loadOwnedBackgrounds() {
  try {
    return JSON.parse(localStorage.getItem("poker_owned_backgrounds") || "[]");
  } catch (e) {
    return [];
  }
}
function saveOwnedBackgrounds(list) {
  localStorage.setItem("poker_owned_backgrounds", JSON.stringify(list));
}

const state = {
  name: localStorage.getItem("poker_name") || "Игрок",
  bank: localStorage.getItem("poker_bank") !== null
    ? parseInt(localStorage.getItem("poker_bank"), 10)
    : 1000,
  lastBonus: localStorage.getItem("poker_last_bonus")
    ? parseInt(localStorage.getItem("poker_last_bonus"), 10)
    : null,
  ownedBackgrounds: loadOwnedBackgrounds(),          // массив id купленных пресетов
  customBgUnlocked: localStorage.getItem("poker_custom_bg_unlocked") === "1",
  customBgData: localStorage.getItem("poker_custom_bg_data") || null,
  selectedBgId: "default",   // выбран в модалке настроек стола (для СЛЕДУЮЩЕГО созданного стола)
  ws: null,
  connectedToTable: false,
  roomCode: null,
  lastState: null,
  myLastKnownChips: null,
  pendingBuyIn: null,   // { type: 'create', background, min_buyin, small_blind, big_blind } или { type: 'join', code }
  pendingConnectAmount: null,
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
    openTableSetupModal();
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

  el("btn-shop").addEventListener("click", () => openShopModal());
  el("shop-close").addEventListener("click", () => el("shop-modal").classList.add("hidden"));
  el("shop-buy-custom").addEventListener("click", buyCustomBgUnlock);
  el("shop-custom-file").addEventListener("change", handleCustomBgUpload);

  el("setup-open-shop").addEventListener("click", () => openShopModal());
  el("setup-confirm").addEventListener("click", confirmTableSetup);
  el("setup-cancel").addEventListener("click", () => el("table-setup-modal").classList.add("hidden"));

  updateBonusButton();
  setInterval(updateBonusButton, 1000);
  registerServiceWorker();
}

// ---------------- Магазин фонов ----------------

function bgById(id) {
  if (id === "default") return DEFAULT_BG;
  if (id === "custom") return { id: "custom", name: "Свой фон" };
  return BG_PRESETS.find((b) => b.id === id) || DEFAULT_BG;
}

function renderBgGrid(container, { selectable, forShop }) {
  container.innerHTML = "";

  const items = [DEFAULT_BG, ...BG_PRESETS];
  items.forEach((bg) => {
    const owned = bg.id === "default" || state.ownedBackgrounds.includes(bg.id);
    const swatch = document.createElement("div");
    swatch.className = "bg-swatch" + (owned ? "" : " locked") +
      (selectable && state.selectedBgId === bg.id ? " selected" : "");
    swatch.style.background = bg.color;
    swatch.textContent = bg.name;

    if (forShop) {
      if (!owned) {
        const tag = document.createElement("div");
        tag.className = "shop-buy-tag";
        tag.textContent = `Купить за ${BG_PRICE.toLocaleString("ru-RU")}`;
        swatch.appendChild(tag);
        swatch.addEventListener("click", () => buyBackground(bg));
      } else {
        const tag = document.createElement("div");
        tag.className = "shop-buy-tag";
        tag.textContent = "Открыт ✓";
        swatch.appendChild(tag);
      }
    } else {
      if (!owned) {
        const lock = document.createElement("div");
        lock.className = "lock-badge";
        lock.textContent = "🔒";
        swatch.appendChild(lock);
      } else if (selectable) {
        swatch.addEventListener("click", () => {
          state.selectedBgId = bg.id;
          renderBgGrid(container, { selectable, forShop });
        });
      }
    }
    container.appendChild(swatch);
  });

  // Свой фон — отдельная плитка, только если уже открыт и загружен
  if (!forShop && state.customBgUnlocked && state.customBgData) {
    const swatch = document.createElement("div");
    swatch.className = "bg-swatch" + (selectable && state.selectedBgId === "custom" ? " selected" : "");
    swatch.style.backgroundImage = `url(${state.customBgData})`;
    swatch.style.backgroundSize = "cover";
    swatch.style.backgroundPosition = "center";
    swatch.textContent = "Свой фон";
    if (selectable) {
      swatch.addEventListener("click", () => {
        state.selectedBgId = "custom";
        renderBgGrid(container, { selectable, forShop });
      });
    }
    container.appendChild(swatch);
  }
}

function openShopModal() {
  renderBgGrid(el("shop-grid"), { selectable: false, forShop: true });
  el("shop-custom-locked").classList.toggle("hidden", state.customBgUnlocked);
  el("shop-custom-unlocked").classList.toggle("hidden", !state.customBgUnlocked);
  el("shop-modal").classList.remove("hidden");
}

function buyBackground(bg) {
  if (state.ownedBackgrounds.includes(bg.id)) return;
  if (state.bank < BG_PRICE) {
    alert(`Не хватает фишек. Нужно ${BG_PRICE.toLocaleString("ru-RU")}, в банке ${state.bank}.`);
    return;
  }
  if (!confirm(`Купить фон «${bg.name}» за ${BG_PRICE.toLocaleString("ru-RU")} фишек?`)) return;
  state.bank -= BG_PRICE;
  saveBank();
  refreshBankDisplay();
  state.ownedBackgrounds.push(bg.id);
  saveOwnedBackgrounds(state.ownedBackgrounds);
  renderBgGrid(el("shop-grid"), { selectable: false, forShop: true });
}

function buyCustomBgUnlock() {
  if (state.bank < CUSTOM_BG_PRICE) {
    alert(`Не хватает фишек. Нужно ${CUSTOM_BG_PRICE.toLocaleString("ru-RU")}, в банке ${state.bank}.`);
    return;
  }
  if (!confirm(`Открыть загрузку своего фона за ${CUSTOM_BG_PRICE.toLocaleString("ru-RU")} фишек?`)) return;
  state.bank -= CUSTOM_BG_PRICE;
  saveBank();
  refreshBankDisplay();
  state.customBgUnlocked = true;
  localStorage.setItem("poker_custom_bg_unlocked", "1");
  el("shop-custom-locked").classList.add("hidden");
  el("shop-custom-unlocked").classList.remove("hidden");
}

function handleCustomBgUpload(ev) {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  const img = new Image();
  const reader = new FileReader();
  reader.onload = () => {
    img.onload = () => {
      // Сжимаем и уменьшаем картинку через canvas, чтобы не забить localStorage
      const maxW = 1280;
      const scale = Math.min(1, maxW / img.width);
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      const ctx = canvas.getContext("2d");
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      const dataUrl = canvas.toDataURL("image/jpeg", 0.7);
      try {
        localStorage.setItem("poker_custom_bg_data", dataUrl);
        state.customBgData = dataUrl;
        alert("Фон загружен и сохранён.");
      } catch (e) {
        alert("Не удалось сохранить изображение — возможно, оно слишком большое. Попробуйте другое, поменьше.");
      }
    };
    img.src = reader.result;
  };
  reader.readAsDataURL(file);
}

function applyTableBackground(bg) {
  if (!bg) { resetBackground(); return; }
  if (bg.type === "custom" && bg.dataUrl) {
    document.body.style.backgroundImage = `url(${bg.dataUrl})`;
    document.body.style.backgroundSize = "cover";
    document.body.style.backgroundPosition = "center";
    document.body.style.backgroundColor = "";
  } else if (bg.type === "solid" && bg.color) {
    document.body.style.backgroundImage = "";
    document.body.style.backgroundColor = bg.color;
  } else {
    resetBackground();
  }
}

function resetBackground() {
  document.body.style.backgroundImage = "";
  document.body.style.backgroundColor = "";
}

// ---------------- Настройки стола (только при создании) ----------------

function openTableSetupModal() {
  state.selectedBgId = "default";
  renderBgGrid(el("setup-bg-grid"), { selectable: true, forShop: false });
  el("setup-min-buyin").value = 0;
  el("setup-max-buyin").value = 0;
  el("setup-small-blind").value = 10;
  el("setup-big-blind").value = 20;
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

  let background = null;
  if (state.selectedBgId === "custom" && state.customBgData) {
    background = { type: "custom", dataUrl: state.customBgData };
  } else if (state.selectedBgId !== "default") {
    const bg = bgById(state.selectedBgId);
    background = { type: "solid", color: bg.color };
  }

  el("table-setup-modal").classList.add("hidden");
  openBuyInModal({
    type: "create",
    background,
    min_buyin: minBuyin,
    max_buyin: maxBuyinRaw,
    small_blind: smallBlind,
    big_blind: bigBlind,
  });
}

// ---------------- Бай-ин (сколько фишек занести за стол, ползунком) ----------------

function openBuyInModal(pending) {
  state.pendingBuyIn = pending;
  el("buyin-bank-max").textContent = state.bank;

  const minAmount = pending.type === "create" ? Math.max(1, pending.min_buyin || 0) : 1;
  let maxAmount = state.bank;
  if (pending.type === "create" && pending.max_buyin) {
    maxAmount = Math.min(pending.max_buyin, state.bank);
  }
  if (maxAmount < minAmount) maxAmount = minAmount; // банк меньше минимума — слайдер выше не даст ошибиться

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
  state.bank -= amount;
  saveBank();
  refreshBankDisplay();
  state.pendingConnectAmount = amount;
  el("buyin-modal").classList.add("hidden");
  el("join-box").classList.add("hidden");

  state.connectedToTable = true;
  showScreen("screen-table");
  el("stage-label").textContent = "Подключение...";

  if (pending.type === "create") {
    connect(() => sendMsg({
      type: "create_room",
      name: state.name,
      chips: amount,
      background: pending.background || null,
      min_buyin: pending.min_buyin || 0,
      max_buyin: pending.max_buyin || 0,
      small_blind: pending.small_blind || 10,
      big_blind: pending.big_blind || 20,
    }));
  } else {
    connect(() => sendMsg({ type: "join_room", code: pending.code, name: state.name, chips: amount }));
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
    alert("Фишки за столом и в банке закончились. Дождитесь дневного бонуса — возвращаемся в меню.");
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
  state.bank -= amount;
  saveBank();
  refreshBankDisplay();
  sendMsg({ type: "rebuy", amount });
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
    if (state.connectedToTable) {
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
  returnToMenu(true); // фишки уже начислены на предыдущей строке — второй раз не начислять
}

function returnToMenu(alreadyCredited) {
  if (!alreadyCredited && state.myLastKnownChips !== null) {
    state.bank += state.myLastKnownChips;
    saveBank();
  }
  state.connectedToTable = false;
  state.lastState = null;
  state.myLastKnownChips = null;
  state.roomCode = null;
  hideRebuyModal();
  stopTurnTimer();
  resetBackground();
  refreshBankDisplay();
  showScreen("screen-menu");
}

// ---------------- Обработка сообщений сервера ----------------

function handleMsg(msg) {
  if (msg.type === "room_created") {
    state.roomCode = msg.code;
    state.pendingConnectAmount = null;
    el("room-code-badge").textContent = `Код: ${msg.code}`;
    el("room-code-badge").classList.remove("hidden");
    applyTableBackground(msg.background);
  } else if (msg.type === "joined") {
    state.roomCode = msg.code;
    state.pendingConnectAmount = null;
    el("room-code-badge").textContent = `Код: ${msg.code}`;
    el("room-code-badge").classList.remove("hidden");
    applyTableBackground(msg.background);
  } else if (msg.type === "error") {
    alert(msg.message);
    if (!state.lastState && state.pendingConnectAmount) {
      // Не удалось сесть за стол (например, не хватает до минимального входа) —
      // возвращаем внесённые фишки в банк и откатываемся в меню.
      state.bank += state.pendingConnectAmount;
      state.pendingConnectAmount = null;
      saveBank();
      if (state.ws) { state.ws.close(); state.ws = null; }
      returnToMenu(true);
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
    state.bank = amount;
    saveBank();
    refreshBankDisplay();
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

// Подстраховка: если вкладку/приложение закрыли прямо во время игры, не
// нажав «Покинуть стол», всё равно сохраняем последний известный стек в банк.
let unloadCreditDone = false;
function creditOnUnload() {
  if (unloadCreditDone) return;
  if (state.connectedToTable && state.myLastKnownChips !== null) {
    unloadCreditDone = true;
    state.bank += state.myLastKnownChips;
    saveBank();
  }
}
window.addEventListener("pagehide", creditOnUnload);
window.addEventListener("beforeunload", creditOnUnload);
