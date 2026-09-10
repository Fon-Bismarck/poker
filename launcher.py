# -*- coding: utf-8 -*-
"""
Лаунчер для ПК: запускает игровой сервер и открывает игру в браузере
по умолчанию. Именно этот файл собирается в .exe (см. README.md).

Телефоны и другие компьютеры в той же Wi-Fi сети заходят на адрес,
который выводится в консоли (http://ВАШ_IP:8000).
"""

import webbrowser
import time
import sys

from server import PokerServer, DEFAULT_PORT, get_local_ip


def main():
    srv = PokerServer(DEFAULT_PORT)
    try:
        srv.start()
    except OSError as e:
        print(f"Не удалось запустить сервер на порту {DEFAULT_PORT}: {e}")
        input("Нажмите Enter для выхода...")
        sys.exit(1)

    ip = get_local_ip()
    url_local = f"http://localhost:{DEFAULT_PORT}"
    url_lan = f"http://{ip}:{DEFAULT_PORT}"

    print("=" * 60)
    print(" ПОКЕР ОНЛАЙН — сервер запущен")
    print("=" * 60)
    print(f" На этом компьютере откройте: {url_local}")
    print(f" Друзья в этой же Wi-Fi сети (ПК или телефон) открывают:")
    print(f"   {url_lan}")
    print(" (Если не открывается — проверьте, что антивирус/фаервол")
    print("  не блокирует Python, и что все устройства в одной сети.)")
    print("=" * 60)
    print(" Не закрывайте это окно, пока идёт игра.")
    print(" Чтобы остановить сервер — закройте это окно.")
    print("=" * 60)

    time.sleep(0.5)
    webbrowser.open(url_local)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
