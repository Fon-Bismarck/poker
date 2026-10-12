/* Общий клиент казино: кошелёк, WebSocket, подключение к столу */
(function () {
  const LS_CHIPS = 'poker_bank', LS_NAME = 'poker_name', START = 1000;  // тот же банк и имя, что в покере

  const C = {
    chips() {
      let v = parseInt(localStorage.getItem(LS_CHIPS), 10);
      if (isNaN(v)) { v = START; localStorage.setItem(LS_CHIPS, v); }
      return v;
    },
    setChips(n) { if (typeof n === 'number' && !isNaN(n)) localStorage.setItem(LS_CHIPS, Math.max(0, Math.floor(n))); },
    name() {
      let n = localStorage.getItem(LS_NAME);
      if (!n) { n = 'Игрок' + Math.floor(100 + Math.random() * 900); }
      return n;
    },
    setName(n) { n = (n || '').trim().slice(0, 16); if (n) localStorage.setItem(LS_NAME, n); },
    fmt(n) { return Number(n).toLocaleString('ru-RU'); },
    short(n) { return n >= 1e6 ? (n / 1e6).toFixed(1).replace('.0', '') + 'M' : n >= 1e4 ? Math.round(n / 1000) + 'k' : n >= 1000 ? (n / 1000).toFixed(1).replace('.0', '') + 'k' : String(n); },
    wsUrl() { return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws'; },

    toast(msg) {
      let box = document.querySelector('.toast-box');
      if (!box) { box = document.createElement('div'); box.className = 'toast-box'; document.body.appendChild(box); }
      const t = document.createElement('div'); t.className = 'toast'; t.textContent = msg; box.appendChild(t);
      setTimeout(() => t.remove(), 2600);
    },

    lost() {
      if (document.querySelector('.overlay')) return;
      const o = document.createElement('div'); o.className = 'overlay';
      o.innerHTML = '<div class="box"><h2>Связь потеряна</h2><p>Соединение с сервером оборвалось. Ваши фишки сохранены.</p><button class="btn gold" id="rc">Переподключиться</button> <a class="btn" href="/menu.html" style="text-decoration:none;display:inline-block">В меню</a></div>';
      document.body.appendChild(o);
      o.querySelector('#rc').onclick = () => location.reload();
    },

    /* Открыть игру: сервер сам найдёт стол */
    open(o) {
      let ws, leaving = false;
      const api = {
        send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); },
        leave(cb) {
          leaving = true;
          let done = false;
          const fin = () => { if (!done) { done = true; cb && cb(); } };
          if (ws && ws.readyState === 1) {
            ws.addEventListener('message', e => { try { if (JSON.parse(e.data).type === 'casino_left') fin(); } catch (_) {} });
            api.send({ type: 'casino_leave' });
            setTimeout(fin, 1200);
          } else fin();
        }
      };
      ws = new WebSocket(C.wsUrl());
      ws.onopen = () => api.send({ type: 'casino_join', game: o.game, name: C.name(), chips: C.chips() });
      ws.onmessage = e => {
        let m; try { m = JSON.parse(e.data); } catch (_) { return; }
        if (m.type === 'casino_joined') o.onJoined && o.onJoined(m);
        else if (m.type === 'casino_state') { if (m.state.you) C.setChips(m.state.you.safe_chips); o.onState(m.state); }
        else if (m.type === 'casino_error') C.toast(m.message);
        else if (m.type === 'casino_left') { if (typeof m.chips === 'number') C.setChips(m.chips); }
      };
      ws.onclose = () => { if (!leaving) C.lost(); };
      return api;
    }
  };
  window.Casino = C;
})();
