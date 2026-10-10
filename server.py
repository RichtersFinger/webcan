"""Definition of `App` with handler registration, a socket-free dispatch
pipeline, and the threaded server.
"""

import logging
import re
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from socketserver import ThreadingTCPServer, StreamRequestHandler
from urllib.parse import unquote, urlsplit
from email.utils import formatdate
import time

from .handlers import Handler, StaticHandler
from .models import Headers, HTTPError, Request, Response
from .routing import Router


_BODYLESS_STATUSES = {204, 304}
"""Status codes whose responses must not carry a body or Content-Length."""


_FALLBACK_CONTENT_TYPE = "application/octet-stream"


_MAX_REQUEST_LINE = 8192
_MAX_HEADER_LINE = 8192
_MAX_HEADERS = 100
"""Hard limits guarding against oversized request lines/headers/bodies."""


_SUPPORTED_METHODS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
}


_TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
"""RFC 7230 header-name token; rejects spaces and control characters."""


_HEADER_VALUE_RE = re.compile(r"[^\x00-\x08\x0a-\x1f\x7f]*")
"""Header value: any octet except CR, LF and other control chars (tab ok)."""


class _ConnectionClosed(Exception):
    """Raised internally when the client closes the connection cleanly."""


class App:
    """A WebCan application including handler registration/routing, request
    dispatch and server start.

    :param log: Logger for server lifecycle logging; defaults to 'webcan'
    :param access_log: Logger for server access logging; defaults to
        'webcan.access'
    :param error_log: Logger for server error logging; defaults to
        'webcan.error'
    :param on_startup: Callback for server startup (called immediately
        before starting to serve).
    :param on_shutdown: Callback for server shutdown (called after server
        has been stopped).
    """

    def __init__(
        self,
        *,
        log: logging.Logger | None = None,
        access_log: logging.Logger | None = None,
        error_log: logging.Logger | None = None,
        on_startup: Callable[["App"], None] | None = None,
        on_shutdown: Callable[["App"], None] | None = None,
    ):
        self.log = log or logging.getLogger("webcan")
        self.access_log = access_log or logging.getLogger("webcan.access")
        self.error_log = error_log or logging.getLogger("webcan.error")
        self._on_startup: Callable[["App"], None] = on_startup or (
            lambda _: None
        )
        self._on_shutdown: Callable[["App"], None] = on_shutdown or (
            lambda _: None
        )
        self._router = Router()

    def set_on_startup(self, callback: Callable[["App"], None]) -> None:
        """Overwrite on-startup callback."""
        self._on_startup = callback

    def set_on_shutdown(self, callback: Callable[["App"], None]) -> None:
        """Overwrite on-shutdown callback."""
        self._on_shutdown = callback

    def register(
        self,
        url_prefix: str | None,
        handler: Handler,
    ) -> None:
        """Register a handler class (no-arg constructed) or instance.

        :param url_prefix: Optional path prefix to prepend to the handler's
            `path` template (must start with '/'; defaults to '/').
        :param handler: `Handler` instance with a `path` template.
        :raises ValueError: If no methods are resolvable, a method is
            unknown, or `url_prefix` does not start with '/'.
        """
        template = getattr(handler, "path", None)
        if not template:
            raise ValueError(
                f"{type(handler).__name__} must define a 'path' template"
            )

        if url_prefix is not None:
            if not url_prefix.startswith("/"):
                raise ValueError(
                    f"URL prefix must start with '/', got {url_prefix!r}"
                )
            prefix = url_prefix.rstrip("/")
            # Avoid a double slash if template is '/' or starts with '/'
            if template.startswith("/"):
                template = f"{prefix}{template}"
            else:
                template = f"{prefix}/{template}"

        methods = sorted(handler.implemented_methods())
        if not methods:
            raise ValueError(
                f"{type(handler).__name__} implements no HTTP methods"
            )

        for method in methods:
            self._router.add(method, template, handler)

    def serve_static(self, url_prefix: str, target: Path) -> None:
        """Serve `target` under `url_prefix` via GET/HEAD.

        A file is served at exactly `url_prefix`; a directory is mounted so
        that `{url_prefix}/relative/path` maps into its tree.

        :param url_prefix: Optional path prefix to prepend to the handler's
            `path` template (must start with '/'; defaults to '/').
        :param target: Path to the target file/directory to serve.
        :raises ValueError: If the prefix is not absolute or `target` is
        missing.
        """
        if not url_prefix.startswith("/"):
            raise ValueError(
                f"Static URL prefix must start with '/', got {url_prefix!r}"
            )
        handler = StaticHandler(target)
        template = (
            url_prefix
            if target.is_file()
            else f"{url_prefix.rstrip('/')}/{{filepath:multisegment}}"
        )
        self._router.add("GET", template, handler)

    def dispatch(self, request: Request) -> Response:
        """Run the full request pipeline; never raises.

        Converts :class:`HTTPError` to plain-text responses, turns any other
        exception into a logged 500, and emits one INFO log line per request.
        """
        start = time.perf_counter()
        try:
            response = self._handle(request)
        except HTTPError as error:
            response = error.to_response()
        # deliberately broad catch at top-level
        # pylint: disable=broad-exception-caught
        except Exception as error:
            self.error_log.exception(
                "Unhandled error for %s %s: %s",
                request.method,
                request.path,
                error,
            )
            response = HTTPError(500).to_response()

        duration_ms = (time.perf_counter() - start) * 1000
        self.access_log.info(
            '%s "%s %s" %d %s %.1fms host=%s ua=%s',
            request.client_ip,
            request.method,
            request.path,
            response.status,
            (
                response.content_length()
                if response.content_length() is not None
                else "-"
            ),
            duration_ms,
            request.headers.get("Host", "-"),
            request.headers.get("User-Agent", "-"),
        )
        return response

    def run(self, host: str = "127.0.0.1", port: int = 8000) -> None:
        """Serve forever on a threaded TCP server (production entry point)."""
        # dynamically create class with property `app: App`
        handler_cls = type(
            "BoundRequestHandler", (_AppRequestHandler,), {"app": self}
        )
        with _Server((host, port), handler_cls) as server:
            self._on_startup(self)
            self.log.info("Serving on http://%s:%d", host, port)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                self.log.warning("Shutting down (Ctrl-C again to force)")
            finally:
                try:
                    server.shutdown()
                except KeyboardInterrupt:
                    self.log.warning("Forced shutdown")
                finally:
                    try:
                        self._on_shutdown(self)
                    # pylint: disable=broad-exception-caught
                    except Exception:
                        self.error_log.exception("Shutdown callback failed")

    def _handle(self, request: Request) -> Response:
        resolution = self._router.resolve(request.path)
        request.path_params = resolution.path_params

        method = request.method.upper()
        if method == "OPTIONS":
            return Response(
                status=204, headers={"Allow": resolution.allow_header}
            )

        lookup = "GET" if method == "HEAD" else method
        handler = resolution.handlers.get(lookup)
        if handler is None:
            raise HTTPError(405, headers={"Allow": resolution.allow_header})
        return handler.handle(request)


class _Server(ThreadingTCPServer):
    """Threaded TCP server with sane restart/shutdown defaults."""

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = True


class _AppRequestHandler(StreamRequestHandler):
    """Strict HTTP/1.1 parser wiring the socket to :meth:`App.dispatch`.

    One instance per connection; serves sequential requests (keep-alive).
    Chunked and folded requests are rejected outright to avoid smuggling.
    """

    timeout = 30
    app: App

    _MANAGED_HEADERS = {
        "server",
        "date",
        "connection",
        "content-length",
        "transfer-encoding",
    }

    def handle(self) -> None:
        """Serve requests on this connection until close, error or timeout."""
        while self._handle_one():
            pass

    def _handle_one(self) -> bool:
        try:
            request, keep_alive = self._read_request()
        except _ConnectionClosed:
            return False
        except (ConnectionError, TimeoutError):
            return False
        except HTTPError as error:
            self.app.error_log.warning(
                "Rejected request from %s: %d %s",
                self.client_address[0],
                error.status,
                error.message,
            )
            self._safe_write(error.to_response(), "GET", keep_alive=False)
            return False
        response = self.app.dispatch(request)
        wrote = self._safe_write(response, request.method, keep_alive)
        return keep_alive and wrote

    def _read_request(self) -> tuple[Request, bool]:
        raw = self._read_line(_MAX_REQUEST_LINE)
        if not raw:
            raise _ConnectionClosed
        line = raw.rstrip(b"\r\n")
        if not line:
            raise _ConnectionClosed
        try:
            method, target, version = line.decode("latin-1").split(" ")
        except ValueError as e:
            raise HTTPError(400, "Malformed request line") from e
        if version != "HTTP/1.1":
            raise HTTPError(505, "Only HTTP/1.1 supported")
        if method not in _SUPPORTED_METHODS:
            raise HTTPError(501, "Unsupported method")

        headers = self._read_headers()
        if sum(n.lower() == "host" for n in headers) != 1:
            raise HTTPError(400, "Exactly one Host header required")
        keep_alive = self._keep_alive(headers)
        content_length, read_body = self._get_read_body(headers)

        if any(
            ord(c) < 0x20 or ord(c) == 0x7F for c in (target + unquote(target))
        ):
            raise HTTPError(400, "Invalid request target")
        split = urlsplit(target)
        request = Request(
            method=method,
            path=split.path,
            client_ip=self.client_address[0],
            query_string=split.query,
            headers=headers,
            read_body=read_body,
            content_length=content_length,
        )
        return request, keep_alive

    def _read_line(self, limit: int) -> bytes:
        raw = self.rfile.readline(limit + 1)
        if len(raw) > limit:
            raise HTTPError(431, "Request line or header too long")
        return raw

    def _read_headers(self) -> Headers:
        items: list[tuple[str, str]] = []
        while True:
            raw = self._read_line(_MAX_HEADER_LINE)
            if not raw:
                raise HTTPError(400, "Unexpected end of headers")
            line = raw.rstrip(b"\r\n")
            if not line:
                break
            if len(items) >= _MAX_HEADERS:
                raise HTTPError(431, "Too many headers")
            if line[:1] in (b" ", b"\t"):
                raise HTTPError(400, "Header line folding is not supported")
            name, sep, value = line.partition(b":")
            if not sep:
                raise HTTPError(400, "Malformed header line")
            try:
                name_s = name.decode("ascii")
                value_s = value.decode("latin-1").strip()
            except UnicodeDecodeError as e:
                raise HTTPError(400, "Invalid header encoding") from e
            if not _TOKEN_RE.fullmatch(name_s):
                raise HTTPError(400, "Invalid header name")
            if not _HEADER_VALUE_RE.fullmatch(value_s):
                raise HTTPError(400, "Invalid header value")
            items.append((name_s, value_s))

        if sum(n.lower() == "content-length" for n, _ in items) > 1:
            raise HTTPError(400, "Duplicate Content-Length header")

        return Headers(items)

    def _get_read_body(
        self, headers: Headers
    ) -> tuple[int, Callable[[int | None], bytes]]:
        """Returns tuple of request's Content-Length and read body-callback."""
        if headers.get("Transfer-Encoding"):
            raise HTTPError(501, "Transfer-Encoding is not supported")
        length_header = headers.get("Content-Length")
        if length_header is None:
            return 0, lambda length: b""
        try:
            request_length = int(length_header)
        except ValueError as e:
            raise HTTPError(400, "Invalid Content-Length header") from e
        if request_length < 0:
            raise HTTPError(400, "Negative Content-Length")

        accum_length = 0

        def _read_body(length: int | None = None) -> bytes:
            nonlocal accum_length
            length_to_read = min(
                length or request_length, request_length - accum_length
            )
            if length_to_read <= 0:
                return b""

            accum_length += length_to_read
            part = self.rfile.read(length_to_read)
            if len(part) < length_to_read:
                raise _ConnectionClosed
            return part

        return request_length, _read_body

    @staticmethod
    def _keep_alive(headers: Headers) -> bool:
        connection = (headers.get("Connection") or "").lower()
        return "close" not in connection

    def _safe_write(
        self, response: Response, method: str, keep_alive: bool
    ) -> bool:
        try:
            self._write_response(response, method, keep_alive)
            self.wfile.flush()
            return True
        except (_ConnectionClosed, ConnectionError, TimeoutError):
            return False
        except OSError:
            self.app.error_log.exception(
                "I/O error writing response to %s", method
            )
            return False

    def _write_response(
        self, response: Response, method: str, keep_alive: bool
    ) -> None:
        length = response.content_length()
        # Iterator bodies may still declare a length (e.g. byte ranges).
        declared = response.headers.get("Content-Length")
        if length is None and declared is not None:
            length = int(declared)

        headers = {
            n: v
            for n, v in response.headers.items()
            if n.lower() not in self._MANAGED_HEADERS
        }
        incoming_headers = {n.lower() for n in headers}
        body_allowed = (
            method.upper() != "HEAD"
            and response.status not in _BODYLESS_STATUSES
        )
        use_chunked = body_allowed and length is None

        lines = [
            f"HTTP/1.1 {response.status} {self._reason(response.status)}",
            "Server: WebCan",
            f"Date: {formatdate(usegmt=True)}",
        ]
        lines.extend(f"{name}: {value}" for name, value in headers.items())
        if response.status not in _BODYLESS_STATUSES:
            if use_chunked:
                lines.append("Transfer-Encoding: chunked")
            elif length is not None:
                lines.append(f"Content-Length: {length}")
            if length != 0 and "content-type" not in incoming_headers:
                lines.append(f"Content-Type: {_FALLBACK_CONTENT_TYPE}")
        lines.append(
            "Connection: keep-alive" if keep_alive else "Connection: close"
        )
        self.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))

        if body_allowed:
            self._write_body(response, method, length, use_chunked)

    def _write_body(
        self,
        response: Response,
        method: str,
        length: int | None,
        use_chunked: bool,
    ) -> None:
        try:
            if use_chunked:
                for chunk in response.iter_body():
                    if chunk:
                        self.wfile.write(
                            f"{len(chunk):x}\r\n".encode("ascii")
                            + chunk
                            + b"\r\n"
                        )
                self.wfile.write(b"0\r\n\r\n")
            else:
                written = 0
                for chunk in response.iter_body():
                    written += len(chunk)
                    self.wfile.write(chunk)
                if length is not None and written != length:
                    # Declared length lied: drop the connection to avoid desync.
                    raise OSError(
                        f"body length {written} != declared {length}"
                    )
        except (ConnectionError, TimeoutError):
            raise
        # pylint: disable=broad-exception-caught
        except Exception as exc_info:
            self.app.error_log.exception(
                "Error while streaming body to %s", method
            )
            raise _ConnectionClosed from exc_info

    @staticmethod
    def _reason(status: int) -> str:
        try:
            return HTTPStatus(status).phrase
        except ValueError:
            return ""
