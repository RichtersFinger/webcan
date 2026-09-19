"""Handler base class and the built-in static file handler."""

import abc
import calendar
from collections.abc import Iterator
from email.utils import formatdate, parsedate
from pathlib import Path

from .models import STREAM_CHUNK_SIZE, HTTPError, Request, Response


ACCEPTED_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE")
"""HTTP methods that can be implemented on and registered for a handler.

HEAD and OPTIONS are answered automatically by the dispatch pipeline.
"""


_MAX_CONTENT_LENGTH = 10 * 1024 * 1024


class Handler(abc.ABC):
    """Base class for request handlers bound to a path template.

    Subclasses set `path` (e.g. `"/users/{user_id}"`) and override any of
    :meth:`get`, :meth:`post`, :meth:`put`, :meth:`patch`, :meth:`delete`.
    Calling unimplemented methods yields 405-error; HEAD is served via
    :meth:`get`. Custom constructors need to call base class constructor.

    :param max_content_length: Upper limit for request body size.
    """

    path: str

    def __init__(
        self, *, max_content_length: int = _MAX_CONTENT_LENGTH, **_
    ) -> None:
        self.max_content_length = max_content_length

    @classmethod
    def implemented_methods(cls) -> set[str]:
        """HTTP methods for which this class overrides the base hook."""
        return set(
            method
            for method in ACCEPTED_METHODS
            if getattr(cls, method.lower())
            is not getattr(Handler, method.lower())
        )

    def handle(self, request: Request) -> Response:
        """Dispatch `request` to the hook matching its HTTP method.

        :raises HTTPError: 405 for methods without a hook.
        """
        if request.content_length > self.max_content_length:
            raise HTTPError(413, "Request body too large")

        method = request.method.upper()
        hook_name = "get" if method == "HEAD" else method.lower()
        if method != "HEAD" and method not in ACCEPTED_METHODS:
            raise HTTPError(405)

        return getattr(self, hook_name)(request)

    def get(self, request: Request) -> Response:
        """Handle GET; override in subclasses."""
        raise HTTPError(405)

    def post(self, request: Request) -> Response:
        """Handle POST; override in subclasses."""
        raise HTTPError(405)

    def put(self, request: Request) -> Response:
        """Handle PUT; override in subclasses."""
        raise HTTPError(405)

    def patch(self, request: Request) -> Response:
        """Handle PATCH; override in subclasses."""
        raise HTTPError(405)

    def delete(self, request: Request) -> Response:
        """Handle DELETE; override in subclasses."""
        raise HTTPError(405)


class StaticHandler(Handler):
    """Serves a single file, or files from a directory tree, via GET/HEAD.

    Directory serving expects the mount template to provide a `filepath`
    path parameter. Requests that escape the base directory answer 404.

    :param target: Existing file or directory to serve.
    :param content_type: Serve files with explicit MIME type.
    :raises ValueError: If `target` does not exist.
    """

    def __init__(
        self, target: Path, content_type: str | None = None, **kwargs
    ):
        super().__init__(**kwargs)
        if not target.exists():
            raise ValueError(f"Static target does not exist: {target}")
        self._target = target.resolve()
        self._content_type = content_type

    def get(self, request: Request) -> Response:
        """Serve the resolved file with validators, conditional GET and single
        byte-range support (honouring `If-Range`)."""
        path = self._resolve_path(request)
        if not path.is_file():
            raise HTTPError(404)
        stat = path.stat()
        etag = f'"{stat.st_size:x}-{int(stat.st_mtime):x}"'
        validators = {
            "ETag": etag,
            "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
        }

        if self._is_not_modified(request, etag, stat.st_mtime):
            return Response(status=304, headers=validators)

        span = None
        if self._range_applicable(request, etag, stat.st_mtime):
            span = self._parse_range(request.headers["Range"], stat.st_size)
        if span is None:
            return Response.file(path, self._content_type, headers=validators)

        start, end = span
        length = end - start + 1
        headers = {
            "Content-Type": self._content_type
            or Response.guess_content_type(path),
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{stat.st_size}",
            "Accept-Ranges": "bytes",
            **validators,
        }
        return Response(
            status=206,
            headers=headers,
            body=self._iter_file_range(path, start, length),
        )

    def _resolve_path(self, request: Request) -> Path:
        if self._target.is_file():
            return self._target
        candidate = (
            self._target / request.path_params.get("filepath", "")
        ).resolve()
        if not candidate.is_relative_to(self._target):
            # Path traversal attempt (e.g. "../../etc/passwd")
            raise HTTPError(404)
        return candidate

    def _is_not_modified(
        self, request: Request, etag: str, mtime: float
    ) -> bool:
        """Evaluate conditional GET headers. `If-None-Match` takes precedence
        over `If-Modified-Since` (RFC 9110)."""
        if_none_match = request.headers.get("If-None-Match")
        if if_none_match is not None:
            candidates = [t.strip() for t in if_none_match.split(",")]
            return "*" in candidates or any(
                self._etag_match(c, etag) for c in candidates
            )
        if_modified_since = request.headers.get("If-Modified-Since")
        if if_modified_since:
            parsed = parsedate(if_modified_since)
            if parsed is not None:
                return int(mtime) <= calendar.timegm(parsed)
        return False

    def _range_applicable(
        self, request: Request, etag: str, mtime: float
    ) -> bool:
        """Whether a `Range` should be served, honouring `If-Range`."""
        if "Range" not in request.headers:
            return False
        if_range = request.headers.get("If-Range")
        if if_range is None:
            return True
        if if_range.startswith(('"', "W/")):
            return self._etag_match(if_range, etag)
        parsed = parsedate(if_range)
        return parsed is not None and int(mtime) <= calendar.timegm(parsed)

    @staticmethod
    def _etag_match(candidate: str, etag: str) -> bool:
        """Compare ETags ignoring the weak `W/` prefix."""
        return candidate.removeprefix("W/") == etag.removeprefix("W/")

    def _parse_range(self, header: str, size: int) -> tuple[int, int] | None:
        """Parse a single `bytes=start-end` range into an inclusive `(start, end)`.

        Returns `None` when the header is absent or unsupported (caller serves the
        full file); raises 416 when the range cannot be satisfied.
        """
        unit, sep, spec = header.partition("=")
        if not sep or unit.strip() != "bytes" or "," in spec:
            return (
                None  # absent, unknown unit, or multi-range -> serve full file
            )
        start_s, sep, end_s = spec.partition("-")
        if not sep:
            return None
        try:
            if not start_s:  # suffix range: final N bytes
                if not end_s:
                    return None
                start, end = max(size - int(end_s), 0), size - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
        except ValueError:
            return None
        if start > end or start >= size:
            raise HTTPError(416, headers={"Content-Range": f"bytes */{size}"})
        return start, min(end, size - 1)

    def _iter_file_range(
        self, path: Path, start: int, length: int
    ) -> Iterator[bytes]:
        """Yield exactly `length` bytes from `path` starting at `start`."""
        remaining = length
        with path.open("rb") as file:
            file.seek(start)
            while remaining > 0 and (
                chunk := file.read(min(STREAM_CHUNK_SIZE, remaining))
            ):
                remaining -= len(chunk)
                yield chunk
