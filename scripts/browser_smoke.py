r"""Run native Chromium fetch against isolated real backend/Vite servers.

Usage from the project root: .\venv\Scripts\python.exe scripts/browser_smoke.py
No browser automation dependency or external service is required.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url, process):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Test service exited: {url}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {url}")


def main():
    browsers = [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"]
    browser = next((path for path in browsers if path.is_file()), None)
    if browser is None:
        raise RuntimeError("Chrome or Edge is required for native browser verification.")
    runtime = (ROOT / "backend/runtime").resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    profile = runtime / f"browser-smoke-{uuid4().hex}"
    profile.mkdir(mode=0o777)
    api_port, web_port = free_port(), free_port()
    api, web = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{web_port}"
    processes = []
    hidden = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        with (runtime / "browser-smoke-services.log").open("w", encoding="utf-8") as log:
            backend = subprocess.Popen([sys.executable, "-m", "uvicorn", "scripts.browser_smoke_server:app",
                "--host", "127.0.0.1", "--port", str(api_port)], cwd=ROOT, stdout=log, stderr=log, **hidden)
            processes.append(backend)
            vite = subprocess.Popen([shutil.which("node") or "node", "node_modules/vite/bin/vite.js",
                "--host", "127.0.0.1", "--port", str(web_port), "--strictPort"],
                cwd=ROOT / "frontend", env={**os.environ, "VITE_API_BASE": api}, stdout=log, stderr=log, **hidden)
            processes.append(vite)
            wait_for(api + "/api/health", backend)
            wait_for(web + "/browser-smoke.html", vite)
            run = subprocess.run([str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", "--disable-extensions", f"--user-data-dir={profile}",
                "--window-size=1600,1100", f"--screenshot={runtime / 'browser-smoke-feedback.png'}",
                "--virtual-time-budget=120000", "--timeout=120000", "--dump-dom",
                f"{web}/browser-smoke.html?api={api}"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=150, **hidden)
            (runtime / "browser-smoke-result.html").write_text(run.stdout, encoding="utf-8")
            if 'id="result" data-status="passed"' not in run.stdout:
                print(run.stdout[-12000:])
                print(run.stderr[-3000:], file=sys.stderr)
                raise RuntimeError("Native browser smoke test failed; inspect backend/runtime/browser-smoke-result.html")
            import html
            import re
            result = re.search(r'<pre id="result" data-status="passed">(.*?)</pre>', run.stdout, re.S)
            print(json.dumps(json.loads(html.unescape(result.group(1))), indent=2))
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        # The unique, owned browser profile is the only recursive cleanup target.
        resolved = profile.resolve()
        if resolved.is_relative_to(runtime) and resolved.name.startswith("browser-smoke-"):
            shutil.rmtree(resolved, ignore_errors=True)


if __name__ == "__main__":
    main()
