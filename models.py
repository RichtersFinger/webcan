"""HTTP primitives:
* request/response models,
* headers,
* and custom errors.
"""

from __future__ import annotations

import json
import re
import mimetypes
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from email.utils import formatdate


STREAM_CHUNK_SIZE = 64 * 1024
"""Streaming chunk size."""


_HEADER_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
"""RFC 7230 header-name token; rejects spaces and control characters."""


_HEADER_VALUE_RE = re.compile(r"[^\x00-\x08\x0a-\x1f\x7f]*")
"""Header value: any octet except CR, LF and other control chars (tab ok)."""


DEFAULT_ERROR_MESSAGES: dict[int, str] = {
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
}
"""Mapping of internally used status codes to default text messages."""


Body = bytes | str | Path | Iterator[bytes]
"""Accepted types for `Response.body`. Note that `Path`s and `Iterator`s
are streamed.
"""


_MIN_ERROR_STATUS = 400
_MAX_ERROR_STATUS = 599


def _default_error_message(status: int) -> str:
    if status in DEFAULT_ERROR_MESSAGES:
        return DEFAULT_ERROR_MESSAGES[status]
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


class HTTPError(Exception):
    """Signals an HTTP error that is rendered as a plain-text response.

    :param status: HTTP status code in the 400-599 range.
    :param message: Plain-text body; defaults to a canonical message for the status.
    :param headers: Extra response headers (e.g. `Allow` for 405).
    :raises ValueError: If `status` is not an error status code.
    """

    def __init__(
        self,
        status: int,
        message: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        if not _MIN_ERROR_STATUS <= status <= _MAX_ERROR_STATUS:
            raise ValueError(
                f"HTTPError status must be in 400-599, got {status}"
            )
        self.status = status
        self.message = (
            message if message is not None else _default_error_message(status)
        )
        self.headers = headers or {}
        super().__init__(f"{status} {self.message}")

    def to_response(self) -> Response:
        """Render this error as a plain-text :class:`Response`."""
        headers = {"Content-Type": "text/plain; charset=utf-8", **self.headers}
        return Response(status=self.status, headers=headers, body=self.message)


class Headers(Mapping[str, str]):
    """Case-insensitive, read-only mapping of HTTP header names to values.

    :param items: Mapping or iterable of `(name, value)` pairs; latest wins.
    """

    def __init__(
        self, items: Mapping[str, str] | Iterable[tuple[str, str]] = ()
    ):
        pairs = items.items() if isinstance(items, Mapping) else items
        self._entries: dict[str, tuple[str, str]] = {
            name.lower(): (name, value) for name, value in pairs
        }

    def __getitem__(self, name: str) -> str:
        return self._entries[name.lower()][1]

    def __iter__(self) -> Iterator[str]:
        return (original for original, _ in self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"Headers({dict(self.items())!r})"


@dataclass
class Request:
    """An incoming HTTP request (fully buffered; no streaming)."""

    method: str
    path: str
    client_ip: str = field(default_factory=lambda: "")
    query_string: str = field(default_factory=lambda: "")
    headers: Headers = field(default_factory=Headers)
    body: bytes = field(default_factory=lambda: b"")
    path_params: dict[str, str] = field(default_factory=dict)

    @cached_property
    def query_params(self) -> dict[str, list[str]]:
        """Returns parsed query string with all values per parameter name."""
        return parse_qs(self.query_string, keep_blank_values=True)

    @property
    def query(self) -> dict[str, str]:
        """Returns first value per parameter name."""
        return {name: values[0] for name, values in self.query_params.items()}

    @property
    def content_type(self) -> str | None:
        """Returns the `Content-Type` header value, if present."""
        return self.headers.get("Content-Type")

    @cached_property
    def cookies(self) -> dict[str, str]:
        """Returns cookies parsed from the `Cookie` header."""
        parsed = SimpleCookie(self.headers.get("Cookie", ""))
        return {name: morsel.value for name, morsel in parsed.items()}

    def json(self) -> Any:
        """Returns body parsed as JSON.

        :return: The decoded JSON value.
        :raises HTTPError: 400 if the body is not valid JSON.
        """
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPError(400, "Invalid JSON body") from e


@dataclass
class Response:
    """An outgoing HTTP response.

    `Path` and `Iterator[bytes]` bodies are streamed in chunks; iterator
    bodies are sent with chunked transfer encoding.
    """

    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: Body = field(default_factory=lambda: b"")

    @classmethod
    def text(
        cls,
        content: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """Build a `text/plain` response."""
        merged = {
            "Content-Type": "text/plain; charset=utf-8",
            **(headers or {}),
        }
        return cls(status=status, headers=merged, body=content)

    @classmethod
    def json(
        cls,
        data: Any,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """Build an `application/json`-`Response` from any JSON-serializable
        value."""
        merged = {"Content-Type": "application/json", **(headers or {})}
        return cls(status=status, headers=merged, body=json.dumps(data))

    @classmethod
    def no_content(cls) -> Response:
        """Build an empty `204 No Content`-`Response`."""
        return cls(status=204)

    @classmethod
    def redirect(cls, location: str, status: int = 302) -> Response:
        """Build a redirect-`Response` pointing at `location`."""
        return cls(status=status, headers={"Location": location})

    @staticmethod
    def guess_content_type(path: Path) -> str:
        """Guess a MIME type from `path`, adding a UTF-8 charset for text types."""
        ctype = (
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        return ctype

    @classmethod
    def file(
        cls,
        path: Path,
        content_type: str | None = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """Build a streamed file-`Response` with guessed `Content-Type`,
        validators (`ETag`, `Last-Modified`) and `Accept-Ranges`.

        :param path: File to serve.
        :param content_type: Override for the guessed MIME type.
        :param headers: Extra headers; override the defaults on conflict.
        :raises HTTPError: 404 if `path` is not an existing file.
        """
        if not path.is_file():
            raise HTTPError(404)
        resolved_type = content_type or cls.guess_content_type(path)
        stat = path.stat()
        merged = {
            "Content-Type": resolved_type,
            "Content-Length": str(stat.st_size),
            "Accept-Ranges": "bytes",
            "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
            "ETag": f'"{stat.st_size:x}-{int(stat.st_mtime):x}"',
            **(headers or {}),
        }
        return cls(status=status, headers=merged, body=path)

    def __post_init__(self) -> None:
        for name, value in self.headers.items():
            if not _HEADER_NAME_RE.fullmatch(name):
                raise ValueError(f"Invalid response header name: {name!r}")
            if not _HEADER_VALUE_RE.fullmatch(value):
                raise ValueError(f"Invalid response header value for {name!r}")

    def content_length(self) -> int | None:
        """Byte length of the body, or `None` for iterator bodies (unknown
        upfront)."""
        match self.body:
            case bytes() as data:
                return len(data)
            case str() as text:
                return len(text.encode("utf-8"))
            case Path() as path:
                return path.stat().st_size
            case _:
                return None

    def iter_body(self) -> Iterator[bytes]:
        """Yield the body as byte chunks, streaming files and iterators."""
        match self.body:
            case bytes() as data:
                if data:
                    yield data
            case str() as text:
                if text:
                    yield text.encode("utf-8")
            case Path() as path:
                with path.open("rb") as file:
                    while chunk := file.read(STREAM_CHUNK_SIZE):
                        yield chunk
            case _:
                yield from self.body
