"""Start the web app: python -m app [--host 127.0.0.1] [--port 8000] [--no-browser]"""
import argparse
import threading
import webbrowser

import uvicorn


def main():
    p = argparse.ArgumentParser(description="Viral Clipper web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
    print(f"\n  Viral Clipper is running at {url}\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
