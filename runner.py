"""Entry-point helpers: app loading and an optional hot-reload dev supervisor.

The supervisor re-executes the current command (`sys.executable sys.argv`)
in a child process marked by an environment variable; the parent polls file
modification times and restarts the child on change.
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
from threading import Event, Thread
from collections.abc import Sequence
from pathlib import Path

from .server import App


logger = logging.getLogger(__name__)


_RELOAD_CHILD_ENV = "WEBCAN_RELOAD_CHILD"
_TERMINATE_TIMEOUT_SECONDS = 5.0
SKIP_HOT_RELOAD_IN = {
    "__pycache__",
    "venv",
    ".venv",
    ".tox",
    "node_modules",
    ".git",
}
"""Directories in which the dev-running hot-reload is disabled by default."""


def load_app(app_target: str) -> App:
    """Import and return an :class:`App` from a `"module:attribute"` target."""
    module_name, _, attribute = app_target.partition(":")
    if not module_name or not attribute:
        raise ValueError(
            f"app_target must look like 'module:attribute', got {app_target!r}"
        )
    module = importlib.import_module(module_name)
    app = getattr(module, attribute, None)
    if not isinstance(app, App):
        raise TypeError(
            f"{app_target!r} does not resolve to a WebCan App instance"
        )
    return app


def run_dev(
    app_target: str,
    host: str | None = None,
    port: int = 8000,
    *,
    reload: bool = True,
    watch_paths: Sequence[Path | str] | None = None,
    poll_interval: float = 1.0,
) -> None:
    """Run the app for development, optionally restarting on source changes.

    With `reload=False` this is equivalent to `load_app(...).run(...)`.

    :param app_target: Import target, e.g. `"main:app"`.
    :param host: Bind address.
    :param port: Bind port.
    :param reload: Enable the .py-file watcher and process restarts.
    :param watch_paths: Files and directories to watch for changes.
    :param poll_interval: Seconds between watcher polls."""

    host = os.environ.get("WEBCAN_HOST", host or "127.0.0.1")
    port = int(os.environ.get("WEBCAN_PORT", port))

    is_child = os.environ.get(_RELOAD_CHILD_ENV) == "1"
    if not reload or is_child:
        load_app(app_target).run(host, port)
        return

    if watch_paths is None:
        raise ValueError(
            "Hot reload enabled but no paths to watch, call 'run_dev' with "
            "explicit 'watch_paths' kwarg."
        )

    paths = tuple(Path(p) for p in (watch_paths))
    _supervise(host, port, paths, poll_interval)


def _supervise(host: str, port: int, paths: Sequence[Path], poll_interval: float) -> None:
    logger.info(
        "Hot reload active; watching: %s",
        ", ".join(str(p) for p in paths),
    )

    manual_reload = Event()
    def _listen_for_enter():
        print("Press 'Return' to manually reload.")
        for _ in sys.stdin:
            manual_reload.set()

    Thread(target=_listen_for_enter, daemon=True).start()

    process = _spawn_child(host, port)
    try:
        while True:
            reason = _wait_for_change(paths, poll_interval, manual_reload)
            logger.info("Restarting server; Reason: %s \n\n---\n", reason)
            _terminate(process)
            process = _spawn_child(host, port)
    except KeyboardInterrupt:
        logger.info("Stopping dev server")
    finally:
        _terminate(process)


def _wait_for_change(
    paths: Sequence[Path],
    poll_interval: float,
    manual_reload: Event,
) -> str:
    """Block until a watched file changes (returns reason for reload)."""
    baseline = _snapshot(paths)
    while True:
        if manual_reload.wait(poll_interval):
            manual_reload.clear()
            return "manual trigger"

        changed = _first_difference(baseline, _snapshot(paths))
        if changed is not None:
            return f"file '{changed}' changed"


def _snapshot(paths: Sequence[Path]) -> dict[Path, float]:
    """Map every watched file to its mtime, pruning irrelevant directories."""
    mtimes: dict[Path, float] = {}
    for path in paths:
        if path.is_file():
            try:
                mtimes[path] = path.stat().st_mtime
            except OSError:
                pass
        elif path.is_dir():
            stack = [path]
            while stack:
                current = stack.pop()
                try:
                    for p in current.iterdir():
                        if (
                            p.is_dir()
                            and not p.is_symlink()
                            and p.name not in SKIP_HOT_RELOAD_IN
                        ):
                            stack.append(p)
                        elif p.is_file() and p.suffix.lower() == ".py":
                            try:
                                mtimes[p] = p.stat().st_mtime
                            except OSError:
                                pass
                except OSError:
                    pass
    return mtimes


def _first_difference(
    before: dict[Path, float], after: dict[Path, float]
) -> Path | None:
    for path, mtime in after.items():
        if before.get(path) != mtime:
            return path
    for path in before:
        if path not in after:
            return path
    return None


def _spawn_child(host: str, port: int) -> subprocess.Popen[bytes]:
    env = {
        **os.environ,
        _RELOAD_CHILD_ENV: "1",
        "WEBCAN_HOST": host,
        "WEBCAN_PORT": str(port),
    }
    return subprocess.Popen([sys.executable, *sys.argv], env=env)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_TERMINATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning("Server process did not stop gracefully; killing it")
        process.kill()
        process.wait()
