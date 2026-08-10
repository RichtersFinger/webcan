"""Tests for module `webcan.routing`."""

import unittest

from ..handlers import Handler
from ..models import HTTPError, Request, Response
from ..routing import Route, Router


class _Stub(Handler):
    path = "/stub"

    def get(self, request: Request) -> Response:
        return Response.text("ok")


class TestRoute(unittest.TestCase):
    """Test class `Route`."""

    def test_match(self):
        """`Route.match` extracts placeholders or returns None."""
        cases = [
            ("/users", "/users", {}),
            ("/users/{user_id}", "/users/42", {"user_id": "42"}),
            ("/a/{x}/b/{y}", "/a/1/b/2", {"x": "1", "y": "2"}),
            ("/users/{user_id}", "/users/1/extra", None),
            ("/users/{user_id}", "/users/", None),
            (
                "/static/{filepath:multisegment}",
                "/static/css/site.css",
                {"filepath": "css/site.css"},
            ),
        ]
        for template, path, expected in cases:
            with self.subTest(template=template, path=path):
                if expected is None:
                    self.assertIsNone(Route(template).match(path))
                else:
                    self.assertDictEqual(Route(template).match(path), expected)

    def test_literal_special_chars_are_escaped(self):
        """`Route` escapes regex metacharacters in literal segments."""
        route = Route("/files/{name}.json")
        self.assertDictEqual(
            route.match("/files/report.json"), {"name": "report"}
        )
        # the '.' must be literal, not a wildcard
        self.assertIsNone(route.match("/files/reportXjson"))

    def test_invalid_placeholder_name_treated_as_literal(self):
        """Names with leading digits are not valid placeholders."""
        route = Route("/{1abc}")
        # '{1abc}' is matched literally, not as a capture group
        self.assertDictEqual(route.match("/{1abc}"), {})
        self.assertIsNone(route.match("/anything"))

    def test_rejects_relative_template(self):
        """`Route` requires templates to start with '/'."""
        with self.assertRaises(ValueError):
            Route("users/{id}")

    def test_rejects_duplicate_placeholder(self):
        """`Route` rejects templates that reuse a placeholder name."""
        with self.assertRaises(ValueError):
            Route("/{a}/{a}")

    def test_rejects_multisegment_not_final(self):
        """`Route` rejects a :multisegment placeholder before the path end."""
        with self.assertRaises(ValueError):
            Route("/static/{p:multisegment}/")


class TestRouter(unittest.TestCase):
    """Test class `Router`."""

    def test_resolve_returns_handlers_and_params(self):
        """`Router.resolve` returns the method map and extracted parameters."""
        router = Router()
        handler = _Stub()
        router.add("GET", "/users/{user_id}", handler)

        resolution = router.resolve("/users/7")

        self.assertDictEqual(resolution.handlers, {"GET": handler})
        self.assertDictEqual(resolution.path_params, {"user_id": "7"})

    def test_resolve_unknown_path_raises_404(self):
        """`Router.resolve` raises `HTTPError(404)` when nothing matches."""
        with self.assertRaises(HTTPError) as ctx:
            Router().resolve("/nowhere")
        self.assertEqual(ctx.exception.status, 404)

    def test_resolve_first_match_wins(self):
        """`Router.resolve` returns the first template registered."""
        router = Router()
        first, second = _Stub(), _Stub()
        router.add("GET", "/users/{id}", first)
        router.add("GET", "/users/{name}", second)

        resolution = router.resolve("/users/7")

        self.assertDictEqual(resolution.handlers, {"GET": first})
        self.assertDictEqual(resolution.path_params, {"id": "7"})

    def test_multiple_methods_share_template(self):
        """Handlers for the same template are grouped by method."""
        router = Router()
        get_handler, post_handler = _Stub(), _Stub()
        router.add("GET", "/x", get_handler)
        router.add("POST", "/x", post_handler)

        resolution = router.resolve("/x")

        self.assertDictEqual(
            resolution.handlers, {"GET": get_handler, "POST": post_handler}
        )

    def test_method_is_normalized_to_uppercase(self):
        """`Router.add` accepts lowercase method names."""
        router = Router()
        handler = _Stub()
        router.add("get", "/x", handler)
        self.assertDictEqual(router.resolve("/x").handlers, {"GET": handler})

    def test_allow_includes_automatic_head_and_options(self):
        """`allow_header` lists registered methods plus HEAD/OPTIONS for GET."""
        router = Router()
        router.add("GET", "/x", _Stub())
        self.assertEqual(
            router.resolve("/x").allow_header, "GET, HEAD, OPTIONS"
        )

    def test_allow_without_get_omits_head(self):
        """`allow_header` adds OPTIONS but not HEAD when GET is absent."""
        router = Router()
        router.add("POST", "/x", _Stub())
        self.assertEqual(router.resolve("/x").allow_header, "OPTIONS, POST")

    def test_duplicate_registration_rejected(self):
        """`Router.add` rejects a second handler for the same method and
        template.
        """
        router = Router()
        router.add("GET", "/x", _Stub())
        with self.assertRaises(ValueError):
            router.add("GET", "/x", _Stub())

    def test_unknown_method_rejected(self):
        """`Router.add` rejects methods outside the supported set."""
        with self.assertRaises(ValueError):
            Router().add("TRACE", "/x", _Stub())
