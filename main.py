# -*- coding: utf-8 -*-
"""
Точка входа для облачных сред вроде Replit, где по умолчанию запускается
именно main.py. Локально можно так же просто: python main.py
"""

from server import run_server_blocking

if __name__ == "__main__":
    run_server_blocking()
