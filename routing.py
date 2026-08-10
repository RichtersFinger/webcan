"""Routing logic definitions."""

from __future__ import annotations

import re
from urllib.parse import unquote
from dataclasses import dataclass

from .models import HTTPError
from .handlers import ACCEPTED_METHODS, Handler


_PLACEHOLDER_RE = re.compile(
    r"\{(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?P<multisegment>:multisegment)?\}"
)
"""Pattern to match single path parameter-segment like `{user_id}` in
`/users/{user_id}`.

Placeholders need to start with a letter or underscore, contain only letters,
numbers, and underscore. They may end on `:multisegment` to indicate an
internal use for a static file."""


def _compile_template(template: str) -> re.Pattern[str]:
    """Returns regex-pattern built from path template:
    - escape regular path segments
    - inject named capture groups for path parameters
    """
    parts: list[str] = []
    position = 0
    for match in _PLACEHOLDER_RE.finditer(template):
        parts.append(re.escape(template[position : match.start()]))

        # add named capture group for path parameter
        # match entire remaining path if the :multisegment suffix is used
        segment = ".+" if match.group("multisegment") else "[^/]+"
        parts.append(f"(?P<{match.group('name')}>{segment})")

        position = match.end()

        if match.group("multisegment") and position < len(template):
            raise ValueError(
                "A ':multisegment' parameter must be the final segment."
            )

    parts.append(re.escape(template[position:]))
    return re.compile(f"^{''.join(parts)}$")


class Route:
    """Represents a compiled path template such as `/users/{user_id}`.

    :param template: Absolute path template; a placeholder
      - matches only a single path segment
      - contains only alphanumeric and underscore characters (first character
        must not be a number)
      - may be suffixed with `:multisegment` to capture the entire remaining
        path
    :raises ValueError: If the template is not absolute or is malformed.
    """

    def __init__(self, template: str):
        if not template.startswith("/"):
            raise ValueError(
                f"Route template must start with '/', got {template!r}"
            )
        self.template = template
        try:
            self._pattern = _compile_template(template)
        except re.error as e:
            raise ValueError(f"Invalid route template {template!r}") from e

    def match(self, path: str) -> dict[str, str] | None:
        """Return extracted path parameters if `path` matches, else `None`."""
        match = self._pattern.match(path)
        return (
            {k: unquote(v) for k, v in match.groupdict().items()}
            if match
            else None
        )

    def __repr__(self) -> str:
        return f"Route({self.template!r})"


@dataclass
class RoutingResult:
    """Result of a resolved path.

    Stores map of handlers by method and path parameters.
    """

    handlers: dict[str, Handler]
    path_params: dict[str, str]

    @property
    def allow_header(self) -> str:
        """Returns value for the `Allow` header, including automatic
        HEAD/OPTIONS.
        """
        methods = set(self.handlers) | {"OPTIONS"}
        if "GET" in methods:
            methods.add("HEAD")
        return ", ".join(sorted(methods))


class Router:
    """Contains registry of `Route`s and `Handler`s. Resolves request path to
    `RoutingResult`.
    """

    def __init__(self) -> None:
        self._routes: list[Route] = []
        self._handlers: dict[str, dict[str, Handler]] = {}

    def add(self, method: str, template: str, handler: Handler) -> None:
        """Register `Handler` for HTTP-`method` on path `template`.

        :raises ValueError: On unknown methods or duplicate registrations.
        """

        normalized = method.upper()
        if normalized not in ACCEPTED_METHODS:
            raise ValueError(
                f"Unsupported HTTP method {method!r}; "
                f"expected one of {ACCEPTED_METHODS}"
            )

        handlers = self._handlers.get(template)
        if handlers is None:
            handlers = self._handlers[template] = {}
            self._routes.append(Route(template))
        if normalized in handlers:
            raise ValueError(
                f"Duplicate registration for {normalized} {template}"
            )
        handlers[normalized] = handler

    def resolve(self, path: str) -> RoutingResult:
        """Resolve `path` to its `Handler`s.

        First matching template wins (based on insert-order).

        :raises HTTPError: 404 if no template matches.
        """
        for route in self._routes:
            params = route.match(path)
            if params is not None:
                return RoutingResult(
                    handlers=self._handlers[route.template],
                    path_params=params,
                )
        raise HTTPError(404)
