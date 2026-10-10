"""Tests for module `webcan.server`."""

import logging
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from ..handlers import Handler
from ..models import HTTPError, Request, Response
from ..server import App, _Server
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
    app.register(None, _Echo())
    app.register(None, _Exploding())
    app.register(None, _Teapot())
    return TestClient(app)


class TestRegister(unittest.TestCase):
    """Test `Handler` registration."""

    def test_handler_without_hooks_rejected(self):
        """`App.register` rejects handlers that implement no HTTP method."""

        class Empty(Handler):
            path = "/empty"

        with self.assertRaises(ValueError):
            App().register(None, Empty())

    def test_handler_without_path_rejected(self):
        """`App.register` rejects handlers lacking a path template."""

        class NoPath(Handler):
            def get(self, request: Request) -> Response:
                return Response.no_content()

        with self.assertRaises(ValueError):
            App().register(None, NoPath())

    def test_instance_registration_supports_dependencies(self):
        """`App.register` accepts pre-built handler instances."""

        class Greeter(Handler):
            path = "/greet"

            def __init__(self, greeting: str, **kwargs):
                super().__init__(**kwargs)
                self._greeting = greeting

            def get(self, request: Request) -> Response:
                return Response.text(self._greeting)

        app = App()
        app.register(None, Greeter("hello"))
        self.assertEqual(TestClient(app).get("/greet").text, "hello")

    def test_url_prefix_for_path_template(self):
        """`App.register` adds `url_prefix` to path template."""

        app = App()
        app.register("/api", _Echo())
        client = TestClient(app)

        self.assertEqual(client.get("/echo/hello").status, 404)
        self.assertEqual(client.get("/api/echo/hello").text, "hello")


class TestFallback(unittest.TestCase):
    """Test fallback-`Handler` registration."""

    def test_404s_without_fallback(self):
        """No matching `Handler` results in 404."""
        app = App()
        app.register("/api", _Echo())
        client = TestClient(app)

        self.assertEqual(client.get("/echo/hello").status, 404)

    def test_serves_fallback(self):
        """`App.fallback` is served as fallback."""

        class Fallback(Handler):
            path = "/"

            def get(self, request: Request) -> Response:
                return Response.text("OK")

        app = App()
        app.register("/api", _Echo())
        app.fallback(Fallback())
        client = TestClient(app)

        self.assertEqual(client.get("/echo/hello").status, 200)


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


class TestLifecycleCallbacks(unittest.TestCase):
    """Test `on_startup`/`on_shutdown` callbacks of `App.run`."""

    _LOGGER = logging.getLogger("webcan.wire")
    _LOGGER.addHandler(logging.NullHandler())
    _LOGGER.propagate = False

    def _run(self, app: App) -> None:
        with (
            mock.patch.object(
                _Server, "serve_forever", side_effect=KeyboardInterrupt
            ),
            mock.patch.object(_Server, "shutdown"),
        ):
            app.run("127.0.0.1", 0)

    def test_callbacks_run_in_order_with_app(self):
        """`App.run` calls startup, then shutdown, each with the app."""
        events = []
        app = App(
            log=self._LOGGER,
            on_startup=lambda a: events.append(("startup", a)),
            on_shutdown=lambda a: events.append(("shutdown", a)),
        )
        self._run(app)
        self.assertEqual(events, [("startup", app), ("shutdown", app)])

    def test_defaults_are_noops(self):
        """`App.run` works without callbacks."""
        self._run(App(log=self._LOGGER))
