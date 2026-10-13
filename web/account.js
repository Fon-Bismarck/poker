/* Общий модуль аккаунта: токен, вход, сессионное соединение (банк, бонус).
   Подключается на всех страницах. Токен хранится в localStorage. */
(function () {
  const TOKEN_KEY = 'poker_token';
  const wsUrl = () => (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';

  const A = {
    token() { return localStorage.getItem(TOKEN_KEY); },
    setToken(t) { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); },
    info: null,                // последний {username, bank, bonus_left, bonus_amount}
    wsUrl,
    fmt(n) { return Number(n).toLocaleString('ru-RU'); },

    /* Нет токена — отправляем на страницу входа */
    requireLogin() {
      if (!A.token()) { A.toLogin(); return false; }
      return true;
    },
    toLogin() {
      const back = location.pathname + location.search;
      location.replace('/login.html?next=' + encodeURIComponent(back));
    },
    logout() {
      const t = A.token();
      A.setToken(null);
      try {
        const ws = new WebSocket(wsUrl());
        ws.onopen = () => { ws.send(JSON.stringify({ type: 'auth_logout', token: t })); setTimeout(() => ws.close(), 300); };
      } catch (_) {}
      setTimeout(() => location.href = '/login.html', 350);
    },

    /* Постоянное соединение страницы: даёт актуальный банк и бонус.
       opts.onAccount(info), opts.onMessage(msg) */
    session(opts) {
      opts = opts || {};
      let ws, closed = false;
      const api = {
        send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); },
        close() { closed = true; if (ws) ws.close(); }
      };
      function open() {
        ws = new WebSocket(wsUrl());
        ws.onopen = () => api.send({ type: 'auth_token', token: A.token() });
        ws.onmessage = e => {
          let m; try { m = JSON.parse(e.data); } catch (_) { return; }
          if (m.type === 'auth_error') { A.setToken(null); A.toLogin(); return; }
          if (m.type === 'auth_ok' || m.type === 'account') {
            A.info = m.account; opts.onAccount && opts.onAccount(m.account);
            if (m.type === 'auth_ok') opts.onReady && opts.onReady(api);
          }
          opts.onMessage && opts.onMessage(m, api);
        };
        ws.onclose = () => { if (!closed) setTimeout(open, 2000); };
      }
      open();
      return api;
    }
  };
  window.Account = A;
})();
