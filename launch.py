"""
Double-click entry point for the Plant Simulator (run by "Plant Simulator.bat",
or hidden by "Plant Simulator.exe" in the portable build).

It does the three things a person would otherwise have to type in a
terminal: pick a free port, start Streamlit, and open the browser at the
right address once the server is actually listening. Streamlit is run
headless so it never stops to ask the first-run "enter your email"
question in the console - we open the browser ourselves instead.

Close the console window (or press Ctrl+C in it) to stop the simulator.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "app.py"
FIRST_PORT = 8501


def port_in_use(port: int) -> bool:
    """Is something already listening on this port? Checked two ways, because
    on Windows a bind to 127.0.0.1 can succeed while another program (e.g. a
    second copy of this app) is listening on 0.0.0.0 of the same port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def free_port(start: int = FIRST_PORT, tries: int = 50) -> int:
    """First TCP port from `start` upward that nothing is listening on."""
    for port in range(start, start + tries):
        if not port_in_use(port):
            return port
    raise RuntimeError(f"No free port found between {start} and {start + tries - 1}")


def open_browser_when_ready(url: str, port: int, timeout_s: float = 120.0) -> None:
    """Poll until the Streamlit server accepts connections, then open the browser."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(0.4)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                webbrowser.open(url)
                return


def main() -> int:
    os.chdir(HERE)
    if not APP.exists():
        print(f"app.py not found next to this launcher ({HERE}).")
        return 1
    try:
        from streamlit.web import cli as stcli
    except ImportError:
        print("Streamlit is not installed for this Python. Run 'Plant Simulator.bat', which installs it, "
              "or: python -m pip install -r requirements.txt")
        return 1

    port = free_port()
    url = f"http://localhost:{port}"
    # flush=True: when "Plant Simulator.exe" runs this hidden, it reads the
    # URL off this line through a pipe, so it must not sit in a buffer.
    print(f"Plant Simulator starting on {url} - your browser will open in a moment.", flush=True)
    print("Leave this window open while you use the simulator; close it to stop.", flush=True)
    threading.Thread(target=open_browser_when_ready, args=(url, port), daemon=True).start()

    sys.argv = [
        "streamlit", "run", str(APP),
        "--server.port", str(port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--global.developmentMode", "false",
    ]
    return stcli.main()


if __name__ == "__main__":
    sys.exit(main())
