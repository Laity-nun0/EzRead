"""原生窗口启动的脱离进程：保存关闭获准后离线迁库并重新打开。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ezread.data_migration import main

if __name__ == '__main__':
    sys.exit(main())
