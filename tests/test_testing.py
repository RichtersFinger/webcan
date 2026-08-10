"""Tests for module `webcan.testing`."""

import unittest

from ..handlers import Handler
from ..models import Request, Response
from ..server import App
from ..testing import TestClient


class _Inspect(Handler):
    path = "/inspect"

    def get(self, request: Request) -> Response:
        return Response.json(
            {"query": request.query, "header": request.headers.get("X-Token")}
        )

    def post(self, request: Request) -> Response:
        return Response.json(
            {"received": request.json(), "content_type": request.content_type}
        )


class _Named(Handler):
    path = "/items/{name}"

    def get(self, request: Request) -> Response:
        return Response.text(request.path_params["name"])


class TestClientBehaviour(unittest.TestCase):
    """Tests for `TestClient` request building and response capture."""

    def setUp(self):
        app = App()
        app.register(_Inspect())
        self.client = TestClient(app)

    def test_query_and_headers_are_forwarded(self):
        """`TestClient.get` parses the query string and forwards headers."""
        response = self.client.get("/inspect?a=1", headers={"X-Token": "t"})
        self.assertEqual(response.json(), {"query": {"a": "1"}, "header": "t"})

    def test_json_payload_sets_content_type(self):
        """`TestClient.request` encodes `json` and sets the content type."""
        response = self.client.post("/inspect", json={"k": 1})
        self.assertEqual(
            response.json(),
            {"received": {"k": 1}, "content_type": "application/json"},
        )

    def test_body_and_json_are_mutually_exclusive(self):
        """`TestClient.request` rejects passing both `body` and `json`."""
        with self.assertRaises(ValueError):
            self.client.post("/inspect", body=b"x", json={})

    def test_response_headers_are_case_insensitive(self):
        """`TestResponse.headers` supports case-insensitive lookup."""
        response = self.client.get("/inspect")
        self.assertEqual(response.headers["content-type"], "application/json")


class TestClientPathHandling(unittest.TestCase):
    """Tests that `TestClient` forwards the raw path, matching the server."""

    def setUp(self):
        app = App()
        app.register(_Named())
        self.client = TestClient(app)

    def test_percent_encoded_segment_is_unquoted_once(self):
        """The client leaves decoding to the router (no double-unquote)."""
        response = self.client.get("/items/a%2Fb")
        self.assertEqual((response.status, response.text), (200, "a/b"))
