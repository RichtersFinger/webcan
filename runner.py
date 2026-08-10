"""Entry-point helpers: app loading and an optional hot-reload dev supervisor.

The supervisor re-executes the current command (`sys.executable sys.argv`)
in a child process marked by an environment variable; the parent polls file
modification times and restarts the child on change. This is robust against
import caching, unlike in-process module reloading.
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .server import App


logger = logging.getLogger(__name__)


_RELOAD_CHILD_ENV = "WEBCAN_RELOAD_CHILD"
_TERMINATE_TIMEOUT_SECONDS = 5.0
# TODO: make file-watching explicit instead (accepts arguments for what to
# watch)
_SKIP_DIR_NAMES = {"__pycache__", "node_modules", "venv"}


def load_app(app_target: str) -> App:
    """Import and return an :class:`App` from a `"module:attribute"` target.

    :param app_target: Import target, e.g. `"main:app"`.
    :raises ValueError: If the target string is malformed.
    :raises TypeError: If the attribute is not an :class:`App` instance.
    """
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
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    reload: bool = True,
    watch_dirs: Sequence[Path] | None = None,
    poll_interval: float = 0.5,
) -> None:
    """Run the app for development, optionally restarting on source changes.

    With `reload=False` this is equivalent to `load_app(...).run(...)`.

    :param app_target: Import target, e.g. `"main:app"`.
    :param host: Bind address.
    :param port: Bind port.
    :param reload: Enable the file watcher and process restarts.
    :param watch_dirs: Directories to watch for `.py` changes; defaults to CWD.
    :param poll_interval: Seconds between watcher polls.
    """
    is_child = os.environ.get(_RELOAD_CHILD_ENV) == "1"
    if not reload or is_child:
        load_app(app_target).run(host, port)
        return

    directories = tuple(Path(d) for d in (watch_dirs or [Path.cwd()]))
    _supervise(directories, poll_interval)


def _supervise(directories: Sequence[Path], poll_interval: float) -> None:
    logger.info(
        "Hot reload active; watching: %s",
        ", ".join(str(d) for d in directories),
    )
    process = _spawn_child()
    try:
        while True:
            changed = _wait_for_change(process, directories, poll_interval)
            if changed is None:
                sys.exit(process.returncode)
            logger.info("Change detected in %s; restarting server", changed)
            _terminate(process)
            process = _spawn_child()
    except KeyboardInterrupt:
        logger.info("Stopping dev server")
    finally:
        _terminate(process)


def _wait_for_change(
    process: subprocess.Popen[bytes],
    directories: Sequence[Path],
    poll_interval: float,
) -> Path | None:
    """Block until a watched file changes (returns it) or the child exits (returns None)."""
    baseline = _snapshot(directories)
    while True:
        time.sleep(poll_interval)
        if process.poll() is not None:
            return None
        changed = _first_difference(baseline, _snapshot(directories))
        if changed is not None:
            return changed


def _snapshot(directories: Sequence[Path]) -> dict[Path, float]:
    """Map every watched `.py` file to its mtime, pruning irrelevant directories."""
    mtimes: dict[Path, float] = {}
    for directory in directories:
        for root, dirnames, filenames in os.walk(directory):
            dirnames[:] = [
                n
                for n in dirnames
                if n not in _SKIP_DIR_NAMES and not n.startswith(".")
            ]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = Path(root) / filename
                try:
                    mtimes[path] = path.stat().st_mtime
                except OSError:
                    continue  # file vanished between listing and stat
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


def _spawn_child() -> subprocess.Popen[bytes]:
    env = {**os.environ, _RELOAD_CHILD_ENV: "1"}
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
