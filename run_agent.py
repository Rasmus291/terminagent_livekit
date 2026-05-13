"""Auto-restart wrapper for the LiveKit agent.

The livekit-rtc Rust layer has a known panic (ParseIntError in rtc_error.rs)
that crashes the process after SIP calls end. This wrapper automatically
restarts the agent when it crashes.
"""
import os
import py_compile
import subprocess
import sys
import time

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))

AGENT_CMD = [
    sys.executable,
    "-c",
    (
        "import sys, os; "
        "sys.argv = ['main_livekit.py', 'start']; "
        f"os.chdir(r'{AGENT_DIR}'); "
        "sys.path.insert(0, '.'); "
        "from livekit import agents; "
        "from main_livekit import server; "
        "agents.cli.run_app(server)"
    ),
]

# Critical Python files that must compile cleanly before starting
_CRITICAL_FILES = ["main_livekit.py", "config.py", "tool_handler.py", "latency_profiler.py",
                   "session_manager.py", "reporting.py", "contacts_excel.py", "api_server.py"]


def _syntax_check() -> bool:
    """Prüft alle kritischen Dateien auf SyntaxErrors vor dem Start."""
    ok = True
    for fname in _CRITICAL_FILES:
        fpath = os.path.join(AGENT_DIR, fname)
        if not os.path.exists(fpath):
            continue
        try:
            py_compile.compile(fpath, doraise=True)
        except py_compile.PyCompileError as e:
            print(f"[run_agent] ❌ SYNTAX-FEHLER in {fname}: {e}", flush=True)
            ok = False
    return ok


import socket


def _port_free(port: int = 8081) -> bool:
    """Prüft ob der Port frei ist."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False


def main():
    while True:
        if not _syntax_check():
            print("[run_agent] ⚠️ Syntax-Fehler erkannt! Agent wird NICHT gestartet. Retry in 10s...", flush=True)
            time.sleep(10)
            continue
        print("[run_agent] ✅ Syntax-Check bestanden. Starting agent...", flush=True)
        proc = subprocess.run(AGENT_CMD, cwd=AGENT_DIR)
        print(f"[run_agent] Agent exited with code {proc.returncode}. Restarting in 5s...", flush=True)
        time.sleep(5)

if __name__ == "__main__":
    main()
