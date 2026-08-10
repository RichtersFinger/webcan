"""Tests for module `webcan.models`."""

import tempfile
import unittest
from pathlib import Path

from ..models import Headers, HTTPError, Request, Response


class TestHeaders(unittest.TestCase):
    """Test class `Headers`."""

    def test_lookup_is_case_insensitive(self):
        """`Headers.__getitem__` / `get` / `__contains__` ignore name
        casing."""
        headers = Headers({"Content-Type": "text/plain"})
        self.assertEqual(headers["content-type"], "text/plain")
        self.assertEqual(headers.get("CONTENT-TYPE"), "text/plain")
        self.assertIn("cOntent-type", headers)

    def test_iteration_preserves_original_names(self):
        """`Headers` iteration yields the names as originally provided."""
        headers = Headers([("X-Custom", "1")])
        self.assertEqual(list(headers), ["X-Custom"])


class TestRequest(unittest.TestCase):
    """Test class `Request`."""

    def test_query_params_keeps_all_values(self):
        """`Request.query_params` exposes multi-value parameters."""
        request = Request(method="GET", path="/x", query_string="a=1&a=2&b=")
        self.assertEqual(request.query_params, {"a": ["1", "2"], "b": [""]})

    def test_query_returns_first_values(self):
        """`Request.query` flattens to the first value per name."""
        request = Request(method="GET", path="/x", query_string="a=1&a=2")
        self.assertEqual(request.query, {"a": "1"})

    def test_json_parses_body(self):
        """`Request.json` decodes a JSON body."""
        request = Request(method="POST", path="/x", body=b'{"k": 1}')
        self.assertEqual(request.json(), {"k": 1})

    def test_json_invalid_body_raises_400(self):
        """`Request.json` raises `HTTPError(400)` on malformed JSON."""
        request = Request(method="POST", path="/x", body=b"not-json")
        with self.assertRaises(HTTPError) as ctx:
            request.json()
        self.assertEqual(ctx.exception.status, 400)

    def test_cookies_parsed_from_header(self):
        """`Request.cookies` parses the Cookie header."""
        request = Request(
            method="GET", path="/x", headers=Headers({"Cookie": "a=1; b=2"})
        )
        self.assertEqual(request.cookies, {"a": "1", "b": "2"})


class TestHTTPErrorType(unittest.TestCase):
    """Test class `HTTPError`."""

    def test_default_message_for_known_status(self):
        """`HTTPError` falls back to the canonical message per status."""
        self.assertEqual(HTTPError(404).message, "Not Found")

    def test_rejects_non_error_status(self):
        """`HTTPError` rejects status codes outside 400-599."""
        for status in (200, 399, 600):
            with self.subTest(status=status), self.assertRaises(ValueError):
                raise HTTPError(status)

    def test_to_response_is_plain_text_with_extra_headers(self):
        """`HTTPError.to_response` emits text/plain and preserves extra
        headers."""
        response = HTTPError(405, headers={"Allow": "GET"}).to_response()
        self.assertEqual(response.status, 405)
        self.assertEqual(
            response.headers["Content-Type"], "text/plain; charset=utf-8"
        )
        self.assertEqual(response.headers["Allow"], "GET")
        self.assertEqual(response.body, "Method Not Allowed")


class TestResponseModel(unittest.TestCase):
    """Test class `Response`."""

    def test_text_factory_sets_content_type(self):
        """`Response.text` produces a `text/plain` response."""
        response = Response.text("hi", status=201)
        self.assertEqual((response.status, response.body), (201, "hi"))
        self.assertTrue(
            response.headers["Content-Type"].startswith("text/plain")
        )

    def test_json_factory_serializes_data(self):
        """`Response.json` serializes the payload and sets the content type."""
        response = Response.json({"a": 1})
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertEqual(response.body, '{"a": 1}')

    def test_no_content_and_redirect(self):
        """`Response.no_content` is 204; `Response.redirect` sets Location."""
        self.assertEqual(Response.no_content().status, 204)
        redirect = Response.redirect("/elsewhere")
        self.assertEqual(
            (redirect.status, redirect.headers["Location"]),
            (302, "/elsewhere"),
        )

    def test_file_streams_and_sets_headers(self):
        """`Response.file` guesses the type, sets length, and streams
        chunks."""
        with tempfile.TemporaryDirectory() as tmp_path:
            target = Path(tmp_path) / "data.txt"
            target.write_text("payload")

            response = Response.file(target)

            self.assertEqual(response.headers["Content-Length"], "7")
            self.assertTrue(
                response.headers["Content-Type"].startswith("text/plain")
            )
            self.assertEqual(b"".join(response.iter_body()), b"payload")

    def test_file_missing_raises_404(self):
        """`Response.file` raises `HTTPError(404)` for a missing file."""
        with (
            self.assertRaises(HTTPError) as ctx,
            tempfile.TemporaryDirectory() as tmp_path,
        ):
            Response.file(Path(tmp_path) / "missing.bin")
        self.assertEqual(ctx.exception.status, 404)

    def test_content_length_and_iter_body(self):
        """`Response.content_length` and iter_body agree for bytes and str
        bodies.
        """
        cases = [
            (b"abc", 3, b"abc"),
            ("héllo", 6, "héllo".encode("utf-8")),
        ]
        for body, expected_length, expected_bytes in cases:
            with self.subTest(body=body):
                response = Response(body=body)
                self.assertEqual(response.content_length(), expected_length)
                self.assertEqual(
                    b"".join(response.iter_body()), expected_bytes
                )

    def test_iterator_body_has_unknown_length(self):
        """Iterator bodies stream chunks and report no upfront length."""
        response = Response(body=iter([b"a", b"b"]))
        self.assertIsNone(response.content_length())
        self.assertEqual(b"".join(response.iter_body()), b"ab")

    def test_response_rejects_header_injection(self):
        """`Response` construction rejects CRLF in header values."""
        with self.assertRaises(ValueError):
            Response(headers={"X-Test": "a\r\nEvil: 1"})
