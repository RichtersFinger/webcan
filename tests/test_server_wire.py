"""Tests for module `webcan.server` wire protocol: strict HTTP/1.1 parsing, the
hardening limits that guard against smuggling, and response framing."""

import logging
import socket
import threading
import unittest

from ..handlers import _MAX_CONTENT_LENGTH, Handler
from ..models import Request, Response
from ..server import App, _AppRequestHandler, _Server


_LOGGER = logging.getLogger("webcan.wire")
_LOGGER.addHandler(logging.NullHandler())
_LOGGER.propagate = False


class _Ok(Handler):
    path = "/ok"

    def get(self, request: Request) -> Response:
        return Response.text("ok")

    def post(self, request: Request) -> Response:
        return Response.text(str(len(request.body())))


class _Mirror(Handler):
    path = "/mirror"

    def post(self, request: Request) -> Response:
        return Response.text(request.body())


class _Stream(Handler):
    path = "/stream"

    def get(self, request: Request) -> Response:
        return Response(body=iter([b"ab", b"cd"]))


class _Raw(Handler):
    path = "/raw"

    def get(self, request: Request) -> Response:
        return Response(body=b"raw-bytes")


class _Spoof(Handler):
    path = "/spoof"

    def get(self, request: Request) -> Response:
        return Response.text("ok", headers={"Server": "spoofed", "X-E": "1"})


def _read_chunked_body(sock: socket.socket, buffered: bytes) -> bytes:
    """Decode a chunked body, seeding decoding with already-`buffered`
    bytes."""
    data, body = buffered, b""
    while True:
        while b"\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                return body
            data += chunk
        size_line, _, data = data.partition(b"\r\n")
        size = int(size_line, 16)
        if size == 0:
            return body
        while len(data) < size + 2:
            chunk = sock.recv(4096)
            if not chunk:
                return body
            data += chunk
        body += data[:size]
        data = data[size + 2 :]


def _read_response(
    sock: socket.socket, expect_body: bool = True
) -> tuple[int, dict[str, str], bytes]:
    """Read one HTTP response, honouring Content-Length or chunked framing."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.decode().strip().lower()] = value.decode().strip()
    if not expect_body:
        return status, headers, b""
    if headers.get("transfer-encoding") == "chunked":
        body = _read_chunked_body(sock, body)
    else:
        length = int(headers.get("content-length", "0"))
        while len(body) < length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
    return status, headers, body


class _WireTestCase(unittest.TestCase):
    """Base fixture spinning up a real threaded server on a random port."""

    @classmethod
    def setUpClass(cls):
        app = App(log=_LOGGER, access_log=_LOGGER, error_log=_LOGGER)
        for handler in (_Ok(), _Mirror(), _Stream(), _Raw(), _Spoof()):
            app.register(handler)
        handler_cls = type("Bound", (_AppRequestHandler,), {"app": app})
        cls.server = _Server(("127.0.0.1", 0), handler_cls)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def _open(self) -> socket.socket:
        sock = socket.create_connection((self.host, self.port), timeout=0.1)
        self.addCleanup(sock.close)
        return sock

    def _request(
        self, data: bytes, expect_body: bool = True
    ) -> tuple[int, dict[str, str], bytes]:
        sock = self._open()
        sock.sendall(data)
        return _read_response(sock, expect_body)

    def _status(self, data: bytes) -> int:
        return self._request(data)[0]


class TestValidRequests(_WireTestCase):
    """Test valid requests."""

    def test_simple_get_is_served(self):
        """A well-formed GET reaches its handler and returns the body."""
        status, _, body = self._request(
            b"GET /ok HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        self.assertEqual((status, body), (200, b"ok"))

    def test_body_is_read_by_content_length(self):
        """A POST body is delimited by Content-Length and passed intact."""
        status, _, body = self._request(
            b"POST /ok HTTP/1.1\r\nHost: localhost\r\nContent-Length: 5\r\n"
            b"Connection: close\r\n\r\nhello"
        )
        self.assertEqual((status, body), (200, b"5"))


class TestConnectionClosing(_WireTestCase):
    """Test connection close-behavior."""

    def test_http_11_keeps_alive_by_default(self):
        """HTTP/1.1 keeps the connection open by default."""
        _, headers, _ = self._request(b"GET /ok HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertNotEqual(headers.get("connection"), "close")

    def test_keep_alive_serves_sequential_requests(self):
        """HTTP/1.1 keeps the connection open for multiple requests."""
        sock = self._open()
        req = b"GET /ok HTTP/1.1\r\nHost: x\r\n\r\n"
        sock.sendall(req)
        status1, headers1, body1 = _read_response(sock)
        sock.sendall(req)
        status2, _, body2 = _read_response(sock)
        self.assertEqual((status1, body1), (200, b"ok"))
        self.assertEqual((status2, body2), (200, b"ok"))
        self.assertEqual(headers1["connection"], "keep-alive")

    def test_http_10_is_505(self):
        """Only HTTP/1.1 is supported; HTTP/1.0 is rejected."""
        self.assertEqual(self._status(b"GET /ok HTTP/1.0\r\n\r\n"), 505)

    def test_error_closes_connection(self):
        """Parser errors are answered once and close the connection."""
        _, headers, _ = self._request(b"BREW /ok HTTP/1.1\r\n\r\n")
        self.assertEqual(headers["connection"], "close")


class TestRequestLine(_WireTestCase):
    """Test request line parsing."""

    def test_malformed_request_line_is_400(self):
        """A request line without three tokens is rejected."""
        self.assertEqual(
            self._status(b"GET /ok\r\nHost: localhost\r\n\r\n"), 400
        )

    def test_unsupported_method_is_501(self):
        """Unknown HTTP methods are rejected before dispatch."""
        self.assertEqual(
            self._status(b"BREW /ok HTTP/1.1\r\nHost: localhost\r\n\r\n"), 501
        )

    def test_unsupported_version_is_505(self):
        """Only HTTP/1.1 is accepted."""
        self.assertEqual(
            self._status(b"GET /ok HTTP/2.0\r\nHost: localhost\r\n\r\n"), 505
        )

    def test_oversized_request_line_is_431(self):
        """An over-long request line is rejected by the length cap."""
        data = b"GET /" + b"a" * 9000 + b" HTTP/1.1\r\nHost: localhost\r\n\r\n"
        self.assertEqual(self._status(data), 431)


class TestTargetSanitization(_WireTestCase):
    """Reject control characters in the request target."""

    def test_nul_in_path_is_400(self):
        """Rejected null in target."""
        data = b"GET /ok\x00 HTTP/1.1\r\nHost: localhost\r\n\r\n"
        self.assertEqual(self._status(data), 400)

    def test_encoded_nul_in_path_is_400(self):
        """`%00` decodes to NUL and must be rejected after unquote."""
        data = b"GET /ok%00 HTTP/1.1\r\nHost: localhost\r\n\r\n"
        self.assertEqual(self._status(data), 400)

    def test_control_char_in_query_is_400(self):
        """Rejected control char in query."""
        data = b"GET /ok?a=%01 HTTP/1.1\r\nHost: localhost\r\n\r\n"
        self.assertEqual(self._status(data), 400)


class TestHeaders(_WireTestCase):
    """Test header parsing."""

    BASE_DATA = b"POST /ok HTTP/1.1\r\nHost: localhost\r\n"

    def test_oversized_header_line_is_431(self):
        """An over-long header line is rejected by the length cap."""
        data = self.BASE_DATA + b"X: " + b"a" * 9000 + b"\r\n\r\n"
        self.assertEqual(self._status(data), 431)

    def test_too_many_headers_is_431(self):
        """Exceeding the header count cap is rejected."""
        extra = b"".join(b"H%d: 1\r\n" % i for i in range(101))
        data = self.BASE_DATA + extra + b"\r\n"
        self.assertEqual(self._status(data), 431)

    def test_folded_header_is_400(self):
        """Obsolete header line folding is rejected."""
        data = self.BASE_DATA + b"X: a\r\n b\r\n\r\n"
        self.assertEqual(self._status(data), 400)

    def test_missing_colon_is_400(self):
        """A header line without a colon is malformed."""
        self.assertEqual(
            self._status(self.BASE_DATA + b"NoColon\r\n\r\n"), 400
        )

    def test_invalid_header_name_is_400(self):
        """A header name containing a space is rejected as a non-token."""
        self.assertEqual(
            self._status(
                b"GET /ok HTTP/1.1\r\nHost: localhost\r\nBad Name: 1\r\n\r\n"
            ),
            400,
        )


class TestBodyAndSmuggling(_WireTestCase):
    """Test body parsing."""

    BASE_DATA = b"POST /ok HTTP/1.1\r\nHost: localhost\r\n"

    def test_transfer_encoding_is_501(self):
        """Transfer-Encoding is unsupported and rejected (CL/TE guard)."""
        data = self.BASE_DATA + b"Transfer-Encoding: chunked\r\n\r\n"
        self.assertEqual(self._status(data), 501)

    def test_duplicate_content_length_is_400(self):
        """Conflicting duplicate Content-Length headers are rejected."""
        data = (
            self.BASE_DATA + b"Content-Length: 1\r\n"
            b"Content-Length: 2\r\n\r\nx"
        )
        self.assertEqual(self._status(data), 400)

    def test_non_numeric_content_length_is_400(self):
        """A non-numeric Content-Length is rejected."""
        data = self.BASE_DATA + b"Content-Length: abc\r\n\r\n"
        self.assertEqual(self._status(data), 400)

    def test_negative_content_length_is_400(self):
        """A negative Content-Length is rejected."""
        data = self.BASE_DATA + b"Content-Length: -1\r\n\r\n"
        self.assertEqual(self._status(data), 400)

    def test_oversized_body_is_413(self):
        """A Content-Length above the body cap is rejected before reading."""
        data = self.BASE_DATA + b"Content-Length: %d\r\n\r\n" % (
            _MAX_CONTENT_LENGTH + 1
        )
        self.assertEqual(self._status(data), 413)

    def test_no_reading_beyond_size(self):
        """Verify that the Content-Length header is parsed and used by
        the Request object (no reading beyond specified length)."""
        # We use the /mirror handler's POST method which returns the request body
        # as a string.
        body_content = b"test-data"
        data = (
            b"POST /mirror HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 4\r\n"
            b"Connection: close\r\n\r\n" +
            body_content
        )
        status, _, response_body = self._request(data)

        self.assertEqual(status, 200)
        self.assertEqual(response_body, b"test")

    def test_short_read_as_connection_closed(self):
        """A short body is handled as 'connection closed'."""
        data = self.BASE_DATA + b"Content-Length: %d\r\n\r\n" % (
            100
        )
        # the socket read (for the response) times out here because the
        # server drops the connection and does not respond
        with self.assertRaises(TimeoutError):
            self._request(data)


class TestResponseFraming(_WireTestCase):
    """Test outgoing response framing and managed headers."""

    def test_iterator_body_is_chunked(self):
        """An iterator body streams via chunked transfer-encoding."""
        status, headers, body = self._request(
            b"GET /stream HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("transfer-encoding"), "chunked")
        self.assertEqual(body, b"abcd")

    def test_head_omits_body_but_keeps_length(self):
        """HEAD suppresses the body while still declaring Content-Length."""
        status, headers, body = self._request(
            b"HEAD /ok HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
            expect_body=False,
        )
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["content-length"], "2")

    def test_missing_content_type_defaults_to_octet_stream(self):
        """A body without a Content-Type gets the octet-stream fallback."""
        _, headers, body = self._request(
            b"GET /raw HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertEqual(body, b"raw-bytes")

    def test_managed_headers_are_authoritative(self):
        """The server owns Server/Date and preserves custom handler headers."""
        _, headers, _ = self._request(
            b"GET /spoof HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        self.assertEqual(headers["server"], "WebCan")
        self.assertIn("date", headers)
        self.assertEqual(headers["x-e"], "1")
