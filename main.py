#!/usr/bin/env python3
"""同济高等讲堂 自动监控抢课系统

用法:
    python main.py        启动监控抢课（常驻，Ctrl+C 退出）
    python selftest.py    抢课链路自检
"""
from monitor import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已优雅退出，监控已停止。")
