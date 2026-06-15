"""Entry point Relive Studio (dashboard tool). Cổng 7863.

Chạy:  conda activate latentsync && python live_system/webapp/run_studio.py

LƯU Ý: chỉ chạy MỘT engine điều khiển OBS tại một thời điểm (run_studio HOẶC run_live Gradio),
vì cả hai đều có vòng lặp đẩy video lên OBS. Cả hai dùng chung live.db.
"""
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

PORT = 7863


def main():
    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, app_dir=str(HERE), log_level="info")


if __name__ == "__main__":
    main()
