/* Общий клиент казино: подключение к столу. Фишки берутся из банка аккаунта на сервере. */
(function () {
  const C = {
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
      o.innerHTML = '<div class="box"><h2>Связь потеряна</h2><p>Соединение с сервером оборвалось. Ваши фишки в безопасности — они хранятся в аккаунте.</p><button class="btn gold" id="rc">Переподключиться</button> <a class="btn" href="/menu.html" style="text-decoration:none;display:inline-block">В меню</a></div>';
      document.body.appendChild(o);
      o.querySelector('#rc').onclick = () => location.reload();
    },

    /* Открыть игру: сервер сам найдёт стол */
    open(o) {
      if (!Account.requireLogin()) return { send() {}, leave(cb) { cb && cb(); } };
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
      ws.onopen = () => {
        api.send({ type: 'auth_token', token: Account.token() });
        api.send({ type: 'casino_join', game: o.game });
      };
      ws.onmessage = e => {
        let m; try { m = JSON.parse(e.data); } catch (_) { return; }
        if (m.type === 'auth_error' || m.type === 'auth_required') { Account.setToken(null); Account.toLogin(); }
        else if (m.type === 'casino_joined') o.onJoined && o.onJoined(m);
        else if (m.type === 'casino_state') o.onState(m.state);
        else if (m.type === 'casino_error') C.toast(m.message);
      };
      ws.onclose = () => { if (!leaving) C.lost(); };
      return api;
    }
  };
  window.Casino = C;
})();
