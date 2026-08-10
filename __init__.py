"""WebCan: a minimal, stdlib-only, threaded HTTP mini-framework.

Public surface: :class:`App`, :class:`Handler`, :class:`StaticHandler`,
:class:`Request`, :class:`Response`, :class:`Headers`, :class:`HTTPError`,
:class:`TestClient`, :class:`TestResponse`, :func:`run_dev`, :func:`load_app`.
"""

from .handlers import Handler, StaticHandler
from .models import Headers, HTTPError, Request, Response
from .runner import load_app, run_dev
from .server import App
from .testing import TestClient, TestResponse


__version__ = "0.1.0"


__all__ = [
    "App",
    "Handler",
    "Headers",
    "HTTPError",
    "Request",
    "Response",
    "StaticHandler",
    "TestClient",
    "TestResponse",
    "load_app",
    "run_dev",
]
