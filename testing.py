"""In-process test client that exercises the full dispatch pipeline without
sockets.
"""

import json as jsonlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .models import Headers, Request
from .server import App


@dataclass
class TestResponse:
    """Fully buffered response captured by :class:`TestClient`."""

    status: int
    headers: Headers
    body: bytes

    @property
    def text(self) -> str:
        """Body decoded as UTF-8."""
        return self.body.decode("utf-8")

    def json(self) -> Any:
        """Body parsed as JSON."""
        return jsonlib.loads(self.body)


class TestClient:
    """Drives an :class:`App` through :meth:`App.dispatch` for fast,
    deterministic tests.

    :param app: The application under test.
    """

    def __init__(self, app: App):
        self._app = app

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
        json: Any = None,
    ) -> TestResponse:
        """Perform a request against the app.

        :param method: HTTP method name.
        :param path: Request path, optionally with a query string.
        :param headers: Request headers.
        :param body: Raw request body (bytes or UTF-8 encoded string).
        :param json: JSON payload; sets `Content-Type: application/json`.
        :raises ValueError: If both `body` and `json` are given.
        """
        if body is not None and json is not None:
            raise ValueError("Provide either 'body' or 'json', not both")

        header_items = dict(headers or {})
        payload = b""
        if json is not None:
            payload = jsonlib.dumps(json).encode("utf-8")
            header_items.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        elif body is not None:
            payload = body

        split = urlsplit(path)
        request = Request(
            method=method.upper(),
            path=split.path,
            query_string=split.query,
            headers=Headers(header_items),
            body=payload,
        )
        response = self._app.dispatch(request)

        drained = (
            b"" if method.upper() == "HEAD" else b"".join(response.iter_body())
        )
        return TestResponse(
            status=response.status,
            headers=Headers(response.headers),
            body=drained,
        )

    def get(self, path: str, **kwargs: Any) -> TestResponse:
        """Perform a GET request; forwards keyword arguments to
        :meth:`request`.
        """
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> TestResponse:
        """Perform a POST request; forwards keyword arguments to
        :meth:`request`.
        """
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> TestResponse:
        """Perform a PUT request; forwards keyword arguments to
        :meth:`request`.
        """
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> TestResponse:
        """Perform a PATCH request; forwards keyword arguments to
        :meth:`request`.
        """
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> TestResponse:
        """Perform a DELETE request; forwards keyword arguments to
        :meth:`request`.
        """
        return self.request("DELETE", path, **kwargs)

    def head(self, path: str, **kwargs: Any) -> TestResponse:
        """Perform a HEAD request; forwards keyword arguments to
        :meth:`request`.
        """
        return self.request("HEAD", path, **kwargs)

    def options(self, path: str, **kwargs: Any) -> TestResponse:
        """Perform an OPTIONS request; forwards keyword arguments to
        :meth:`request`.
        """
        return self.request("OPTIONS", path, **kwargs)
