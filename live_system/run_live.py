"""Entry point: chạy cả hệ thống live.

- Thread 1 (daemon): LiveController.run_forever() điều phối 24/7.
- Thread chính: Gradio UI cổng 7862.

Chạy:  conda activate latentsync && python run_live.py
"""
import threading

from live_controller import LiveController, setup_logging
from live_ui import create_ui


def main():
    setup_logging()
    controller = LiveController()

    t = threading.Thread(target=controller.run_forever, daemon=True)
    t.start()

    app = create_ui(controller)
    app.launch(server_port=7862, share=False, inbrowser=False)


if __name__ == "__main__":
    main()
