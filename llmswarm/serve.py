"""Serve runner: passive OpenAI-compatible API mode."""
import signal
import sys
import threading

from .server import _make_server


def run_serve(state):
    """Passive OpenAI-compatible API mode: endpoints are the interface."""
    server, _ = _make_server(state)

    def shutdown(signum, frame):
        print(f"\n[serve] Received signal {signum}, shutting down gracefully...",
              file=sys.stderr)
        # serve_forever() runs on the main thread; the signal handler runs on
        # that same thread. Calling server.shutdown() here would block waiting
        # for serve_forever() to finish -- which can never happen from within
        # itself, so the process hangs until the terminal is killed. Instead
        # request the stop from a side thread: it sets the internal flag and the
        # main serve_forever() loop notices it and returns.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    except Exception as e:
        print(f"[serve] Error: {e}", file=sys.stderr)
    finally:
        server.server_close()
        print("[serve] Shutdown complete.", file=sys.stderr)
