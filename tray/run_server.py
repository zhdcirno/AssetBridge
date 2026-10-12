# -*- coding: utf-8 -*-
"""Asset Bridge - relay server launcher used by the tray supervisor.

Why this file exists
--------------------
1. The tray starts the relay server with ``pythonw.exe`` so Windows never
   creates a console window for it at all (a real console window is what the
   user complained about).
2. ``pythonw.exe`` has no stdout/stderr, so this wrapper points both at
   ``logs/server_console.log`` -- otherwise all server output would be lost.
3. Because the server is started detached (``CreateNoWindow`` + no shared
   console), exiting the tray no longer kills the relay service. Only the
   explicit "stop" action does.

Started as::

    pythonw.exe tray\\run_server.py
"""

import os
import runpy
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server", "server.py")
RUNTIME = os.path.join(ROOT, "runtime")
LOG = os.path.join(ROOT, "logs", "server_console.log")


def _redirect_std_streams():
    """Point stdout/stderr at a log file (pythonw has neither)."""
    try:
        logdir = os.path.dirname(LOG)
        if logdir and not os.path.isdir(logdir):
            os.makedirs(logdir)
        fh = open(LOG, "a", encoding="utf-8", buffering=1)
    except Exception:
        return None
    sys.stdout = fh
    sys.stderr = fh
    try:
        fh.write("\n=== %s  relay server start (pythonw, pid=%d) ===\n"
                 % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
        fh.flush()
    except Exception:
        pass
    return fh


def main():
    _redirect_std_streams()
    # server.py parses sys.argv[1:] -> give it exactly what it expects.
    # Extra arguments (e.g. --port 27639 for a test instance) are forwarded.
    sys.argv = [SERVER, "--root", RUNTIME] + list(sys.argv[1:])
    runpy.run_path(SERVER, run_name="__main__")


if __name__ == "__main__":
    main()
