"""Tests for module `webcan.server`."""

import logging
import tempfile
import unittest
from pathlib import Path

from ..handlers import Handler
from ..models import HTTPError, Request, Response
from ..server import App
from ..testing import TestClient


class _Echo(Handler):
    path = "/echo/{word}"

    def get(self, request: Request) -> Response:
        return Response.text(request.path_params["word"])

    def post(self, request: Request) -> Response:
        return Response.json(request.json(), status=201)


class _Exploding(Handler):
    path = "/boom"

    def get(self, request: Request) -> Response:
        raise RuntimeError("kaboom")


class _Teapot(Handler):
    path = "/teapot"

    def get(self, request: Request) -> Response:
        raise HTTPError(418, "short and stout")


def _make_client() -> TestClient:
    logger = logging.getLogger("webcan.test")
    app = App(log=logger, access_log=logger, error_log=logger)
    app.register(_Echo())
    app.register(_Exploding())
    app.register(_Teapot())
    return TestClient(app)


class TestRegister(unittest.TestCase):
    """Test `Handler` registration."""

    def test_explicit_methods_restrict_registration(self):
        """`App.register` with explicit methods exposes only those methods."""
        app = App()
        app.register(_Echo(), methods=["GET"])
        response = TestClient(app).post("/echo/x", json={})
        self.assertEqual(response.status, 405)

    def test_handler_without_hooks_rejected(self):
        """`App.register` rejects handlers that implement no HTTP method."""

        class Empty(Handler):
            path = "/empty"

        with self.assertRaises(ValueError):
            App().register(Empty())

    def test_handler_without_path_rejected(self):
        """`App.register` rejects handlers lacking a path template."""

        class NoPath(Handler):
            def get(self, request: Request) -> Response:
                return Response.no_content()

        with self.assertRaises(ValueError):
            App().register(NoPath())

    def test_instance_registration_supports_dependencies(self):
        """`App.register` accepts pre-built handler instances."""

        class Greeter(Handler):
            path = "/greet"

            def __init__(self, greeting: str):
                self._greeting = greeting

            def get(self, request: Request) -> Response:
                return Response.text(self._greeting)

        app = App()
        app.register(Greeter("hello"))
        self.assertEqual(TestClient(app).get("/greet").text, "hello")


class TestDispatch(unittest.TestCase):
    """Test `App.dispatch`."""

    def setUp(self):
        self.client = _make_client()

    def test_path_params_reach_handler(self):
        """`App.dispatch` passes extracted path parameters to the handler."""
        self.assertEqual(self.client.get("/echo/hi").text, "hi")

    def test_unknown_path_is_plain_text_404(self):
        """`App.dispatch` answers unmatched paths with the default 404 text."""
        response = self.client.get("/missing")
        self.assertEqual((response.status, response.text), (404, "Not Found"))
        self.assertTrue(
            response.headers["Content-Type"].startswith("text/plain")
        )

    def test_unregistered_method_is_405_with_allow(self):
        """`App.dispatch` answers 405 with an Allow header for known paths."""
        response = self.client.delete("/echo/hi")
        self.assertEqual(response.status, 405)
        self.assertEqual(response.headers["Allow"], "GET, HEAD, OPTIONS, POST")

    def test_options_is_automatic(self):
        """`App.dispatch` answers OPTIONS with the Allow header and no body."""
        response = self.client.options("/echo/hi")
        self.assertEqual((response.status, response.body), (204, b""))
        self.assertIn("GET", response.headers["Allow"])

    def test_head_reuses_get_without_body(self):
        """`App.dispatch` serves HEAD via the get hook, body suppressed."""
        response = self.client.head("/echo/hi")
        self.assertEqual((response.status, response.body), (200, b""))

    def test_http_error_from_handler_is_rendered(self):
        """`App.dispatch` renders `HTTPError` raised by handlers as plain
        text."""
        response = self.client.get("/teapot")
        self.assertEqual(
            (response.status, response.text), (418, "short and stout")
        )

    def test_unhandled_exception_becomes_500(self):
        """`App.dispatch` converts unexpected exceptions to a logged 500."""
        with self.assertLogs("webcan.test", level="ERROR"):
            response = self.client.get("/boom")
        self.assertEqual(
            (response.status, response.text), (500, "Internal Server Error")
        )


class TestServeStatic(unittest.TestCase):
    """Test registration of `StaticHandler` through `App.serve_static`."""

    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = Path(tmp_dir.name)

    def test_directory_mount_serves_nested_files(self):
        """`App.serve_static` maps URL suffixes into the directory tree."""
        (self.tmp_path / "css").mkdir()
        (self.tmp_path / "css" / "site.css").write_text("body {}")
        app = App()
        app.serve_static("/static", self.tmp_path)

        response = TestClient(app).get("/static/css/site.css")

        self.assertEqual((response.status, response.text), (200, "body {}"))

    def test_single_file_served_at_exact_prefix(self):
        """`App.serve_static` with a file target serves it at the prefix
        itself."""
        target = self.tmp_path / "robots.txt"
        target.write_text("User-agent: *")
        app = App()
        app.serve_static("/robots.txt", target)

        self.assertEqual(
            TestClient(app).get("/robots.txt").text, "User-agent: *"
        )

    def test_relative_prefix_rejected(self):
        """`App.serve_static` requires an absolute URL prefix."""
        with self.assertRaises(ValueError):
            App().serve_static("static", self.tmp_path)
