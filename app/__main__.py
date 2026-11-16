"""Start the web app: python -m app [--host 127.0.0.1] [--port 8000] [--no-browser]"""
import argparse
import os
import socket
import sys
import threading
import webbrowser


def _free_port(host: str, start: int) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if os.name != "nt":  # match uvicorn: a port in TIME_WAIT after a restart is still usable
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host if host != "0.0.0.0" else "", port))
                return port
            except OSError:
                continue
    return start


def main():
    p = argparse.ArgumentParser(description="Viral Clipper web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    print("\n  Starting Viral Clipper…", flush=True)
    import uvicorn
    from app.config import DATA_DIR
    from app.main import app

    port = _free_port(args.host, args.port)
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{port}"
    print(f"\n  Viral Clipper is running at {url}")
    print(f"  Your clips are saved in {DATA_DIR}")
    if getattr(sys, "frozen", False):
        print("\n  Keep this window open while you use the app. Close it to quit.\n", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
