"""Tests for module `webcan.handlers`."""

import tempfile
import unittest
from email.utils import formatdate
from pathlib import Path

from ..handlers import Handler, StaticHandler
from ..models import Headers, HTTPError, Request, Response


class _ReadWrite(Handler):
    path = "/things"

    def get(self, request: Request) -> Response:
        return Response.text("got")

    def post(self, request: Request) -> Response:
        return Response.text("created", status=201)


class TestHandlerBase(unittest.TestCase):
    """Tests for base-`Handler`."""

    def test_implemented_methods_inferred_from_overrides(self):
        """`Handler.implemented_methods` reflects overridden hooks only."""
        self.assertEqual(_ReadWrite.implemented_methods(), {"GET", "POST"})

    def test_handle_dispatches_to_matching_hook(self):
        """`Handler.handle` routes the request method to the right hook."""
        response = _ReadWrite().handle(Request(method="POST", path="/things"))
        self.assertEqual((response.status, response.body), (201, "created"))

    def test_handle_head_uses_get_hook(self):
        """`Handler.handle` serves HEAD through the `get` hook."""
        response = _ReadWrite().handle(Request(method="HEAD", path="/things"))
        self.assertEqual(response.body, "got")

    def test_unimplemented_method_raises_405(self):
        """Default hooks raise `HTTPError(405)`."""
        with self.assertRaises(HTTPError) as ctx:
            _ReadWrite().handle(Request(method="DELETE", path="/things"))
        self.assertEqual(ctx.exception.status, 405)


class TestStaticHandler(unittest.TestCase):
    """Tests for `StaticHandler` file resolution."""

    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = Path(tmp_dir.name)

    def test_serves_file_from_directory(self):
        """`StaticHandler.get` streams a file addressed by the filepath
        parameter.
        """
        (self.tmp_path / "site.css").write_text("body {}")
        handler = StaticHandler(self.tmp_path)
        request = Request(
            method="GET",
            path="/static/site.css",
            path_params={"filepath": "site.css"},
        )

        response = handler.get(request)

        self.assertEqual(b"".join(response.iter_body()), b"body {}")
        self.assertEqual(
            response.headers["Content-Type"], "text/css; charset=utf-8"
        )

    def test_serves_single_file_target(self):
        """`StaticHandler` bound to a file ignores path parameters."""
        target = self.tmp_path / "favicon.ico"
        target.write_bytes(b"icon")
        response = StaticHandler(target).get(
            Request(method="GET", path="/favicon.ico")
        )
        self.assertEqual(b"".join(response.iter_body()), b"icon")

    def test_path_traversal_answers_404(self):
        """`StaticHandler.get` hides paths escaping the base directory."""
        (self.tmp_path / "inner").mkdir()
        handler = StaticHandler(self.tmp_path / "inner")
        request = Request(
            method="GET",
            path="/static/x",
            path_params={"filepath": "../secret.txt"},
        )
        with self.assertRaises(HTTPError) as ctx:
            handler.get(request)
        self.assertEqual(ctx.exception.status, 404)

    def test_missing_file_answers_404(self):
        """`StaticHandler.get` answers 404 for unknown files."""
        handler = StaticHandler(self.tmp_path)
        request = Request(
            method="GET",
            path="/static/x",
            path_params={"filepath": "nope.txt"},
        )
        with self.assertRaises(HTTPError) as ctx:
            handler.get(request)
        self.assertEqual(ctx.exception.status, 404)

    def test_missing_target_rejected_at_construction(self):
        """`StaticHandler` validates the target at construction time."""
        with self.assertRaises(ValueError):
            StaticHandler(self.tmp_path / "absent")


class _StaticFixture(unittest.TestCase):
    """Shared 10-byte single-file `StaticHandler` fixture."""

    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.target = Path(tmp_dir.name) / "data.txt"
        self.target.write_text("0123456789")
        self.handler = StaticHandler(self.target)
        stat = self.target.stat()
        self.etag = f'"{stat.st_size:x}-{int(stat.st_mtime):x}"'
        self.mtime = stat.st_mtime

    def _get(self, headers: dict[str, str]) -> Response:
        request = Request(
            method="GET", path="/data.txt", headers=Headers(headers)
        )
        return self.handler.get(request)


class TestStaticHandlerConditional(_StaticFixture):
    """Tests for `StaticHandler` conditional GET handling."""

    def test_validators_present_on_full_response(self):
        """A plain GET carries `ETag` and `Last-Modified` validators."""
        response = self._get({})
        self.assertEqual(response.headers["ETag"], self.etag)
        self.assertEqual(
            response.headers["Last-Modified"],
            formatdate(self.mtime, usegmt=True),
        )

    def test_matching_if_none_match_returns_304(self):
        """A matching `If-None-Match` yields 304 with validators."""
        response = self._get({"If-None-Match": self.etag})
        self.assertEqual(response.status, 304)
        self.assertEqual(response.headers["ETag"], self.etag)

    def test_if_none_match_variants_return_304(self):
        """`If-None-Match` matches on wildcard and weak ETags."""
        cases = ["*", f"W/{self.etag}", f'"other", {self.etag}']
        for value in cases:
            with self.subTest(if_none_match=value):
                response = self._get({"If-None-Match": value})
                self.assertEqual(response.status, 304)

    def test_if_modified_since_not_modified_returns_304(self):
        """A future `If-Modified-Since` yields 304."""
        response = self._get(
            {"If-Modified-Since": formatdate(self.mtime + 100, usegmt=True)}
        )
        self.assertEqual(response.status, 304)

    def test_if_none_match_takes_precedence_over_date(self):
        """A non-matching `If-None-Match` wins over a fresh date (RFC 9110)."""
        response = self._get(
            {
                "If-None-Match": '"stale"',
                "If-Modified-Since": formatdate(self.mtime + 100, usegmt=True),
            }
        )
        self.assertEqual(response.status, 200)


class TestStaticHandlerRanges(_StaticFixture):
    """Tests for `StaticHandler` single byte-range serving."""

    def test_range_variants_return_206(self):
        """Explicit, suffix and open-ended ranges stream the right slice."""
        cases = [
            ("bytes=0-3", b"0123", "bytes 0-3/10"),
            ("bytes=-4", b"6789", "bytes 6-9/10"),
            ("bytes=2-", b"23456789", "bytes 2-9/10"),
        ]
        for header, body, content_range in cases:
            with self.subTest(header=header):
                response = self._get({"Range": header})
                self.assertEqual(response.status, 206)
                self.assertEqual(b"".join(response.iter_body()), body)
                self.assertEqual(
                    response.headers["Content-Range"], content_range
                )
                self.assertEqual(
                    response.headers["Content-Length"], str(len(body))
                )

    def test_unsupported_range_serves_full_file(self):
        """Multi-range, unknown units and garbage fall back to the full
        file."""
        cases = ["bytes=0-1,3-4", "items=0-1", "bytes=abc-def"]
        for header in cases:
            with self.subTest(header=header):
                response = self._get({"Range": header})
                self.assertEqual(response.status, 200)
                self.assertEqual(b"".join(response.iter_body()), b"0123456789")

    def test_unsatisfiable_range_returns_416(self):
        """Out-of-bounds or inverted ranges raise 416 with `Content-Range`."""
        for header in ("bytes=20-30", "bytes=5-3"):
            with self.subTest(header=header):
                with self.assertRaises(HTTPError) as ctx:
                    self._get({"Range": header})
                self.assertEqual(ctx.exception.status, 416)
                self.assertEqual(
                    ctx.exception.headers["Content-Range"], "bytes */10"
                )

    def test_if_range_matching_etag_serves_range(self):
        """`If-Range` with a matching ETag serves the partial response."""
        response = self._get({"Range": "bytes=0-3", "If-Range": self.etag})
        self.assertEqual(response.status, 206)

    def test_if_range_stale_etag_serves_full_file(self):
        """`If-Range` with a stale ETag serves the full file."""
        response = self._get({"Range": "bytes=0-3", "If-Range": '"stale"'})
        self.assertEqual(response.status, 200)

    def test_if_range_fresh_date_serves_range(self):
        """`If-Range` with a date newer than mtime serves the range."""
        response = self._get(
            {
                "Range": "bytes=0-3",
                "If-Range": formatdate(self.mtime + 100, usegmt=True),
            }
        )
        self.assertEqual(response.status, 206)
