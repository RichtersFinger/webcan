 ![Tests](https://github.com/RichtersFinger/webcan/actions/workflows/tests.yml/badge.svg?branch=main)

# WebCan

Minimal, stdlib-only, threaded HTTP micro-framework. Zero runtime dependencies and easy to use as git submodule/drop-in package.

## Usage as a git submodule

The repository root is the package; internal imports are relative, so the
import name equals the submodule directory name:

```bash
git submodule add https://github.com/RichtersFinger/webcan webcan
```


## Full demo app

```python
"""Example application demonstrating the WebCan framework."""

import logging
from pathlib import Path

from webcan import App, Handler, HTTPError, Request, Response, run_dev


USERS: dict[str, dict[str, str]] = {
    "1": {"id": "1", "name": "Ada Lovelace"},
    "2": {"id": "2", "name": "Alan Turing"},
}


class UserListHandler(Handler):
    """Collection endpoint: list and create users."""

    path = "/users"

    def get(self, request: Request) -> Response:
        return Response.json(list(USERS.values()))

    def post(self, request: Request) -> Response:
        data = request.json()
        if not isinstance(data, dict) or "name" not in data:
            raise HTTPError(400, "Expected a JSON object with a 'name' field")
        user_id = str(max((int(key) for key in USERS), default=0) + 1)
        user = {"id": user_id, "name": str(data["name"])}
        USERS[user_id] = user
        return Response.json(user, status=201)


class UserHandler(Handler):
    """Single-user endpoint with a path placeholder."""

    path = "/users/{user_id}"

    def get(self, request: Request) -> Response:
        user = USERS.get(request.path_params["user_id"])
        if user is None:
            raise HTTPError(404, "Unknown user")
        return Response.json(user)

    def delete(self, request: Request) -> Response:
        if USERS.pop(request.path_params["user_id"], None) is None:
            raise HTTPError(404, "Unknown user")
        return Response.no_content()


def create_app() -> App:
    """Build and configure the demo application."""
    app_ = App()
    app_.register(
        UserListHandler()
    )
    app_.register(UserHandler())

    app_.serve_static("/", Path("public"))
    return app_


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Dev entry point with hot reload; use app.run(host, port) directly
    # in production.
    run_dev(app, port=8000, reload=True, watch_paths=["main.py"])
    # app.run(host="0.0.0.0", port=8000)
```

## What it offers (and what it doesn't)

Offers:
- HTTP/1.1 with keep-alive, threaded connection handling
- Class-based handlers with path templates (`/users/{id}`, `{path:multisegment}`)
- Automatic HEAD and OPTIONS
- JSON / text / redirect / file responses, streamed bodies
- Static file serving with ETag, Last-Modified, conditional GET and byte ranges
- Strict request parsing (rejects chunked bodies, header folding, oversized input)
- Hot-reload dev server
- a socket-free test client
- Zero runtime dependencies

Doesn't:
- No HTTPS/TLS (put it behind a reverse proxy)
- No HTTP/2 or WebSockets
- No async
- No middleware, sessions, templating, ORM or auth

## For developers

Here is a quick tour:

### The building blocks

- **`models.py`**, HTTP "primitives"
  - `Request`, `Response`,`Headers`, and `HTTPError`
  - essentially these are data records with a bit of convenience like `JSON`-handling.
- **`handlers.py`**
  - a `Handler` describes an API-resource.
  - defines a path template `path`
  - defines all required HTTP verbs `GET`, `POST`, .. (overriding methods `get`, `post`, ...)
  - `StaticHandler` is a built-in handler to serve files or directory trees, including conditional `GET` (ETag / Last-Modified) and single byte-range requests.
- **`routing.py`**
  - `Route` compiles a path template like `/users/{user_id}` into a regex.
  - `Router` keeps the registry and resolves a path to a `RoutingResult` (handlers-by-method + extracted path params).
  - First matching route wins, by insertion order.
- **`server.py`**, wiring:
  - `App` ties registration, routing and dispatch together.
  - internal `_AppRequestHandler` is the strict HTTP/1.1 parser that sits directly at the socket; all of the wire-level logic (parsing, header writing, connection handling) is done here
  - `_Server` is a threaded TCP server
- **`runner.py`**
  - contains `load_app("module:attr")` and the hot-reload dev supervisor (through `run_dev`).
  - reloading works by re-spawning the process as a child passing along settings as environment variables
- **`testing.py`**, `TestClient` drives an `App` through `dispatch` directly, no sockets involved.
  Intended for fast, deterministic tests of the app's handler-wiring.

### Runtime view: a normal request

Here's how the pieces talk during a live request over a socket:

```
socket
  │
  ▼
_AppRequestHandler.handle()          # loops per keep-alive connection
  └─ _handle_one()
       ├─ _read_request()            # parse request line, headers, body
       │    └─ builds Request
       └─ App.dispatch(request)      # never raises; logs one line
            └─ App._handle(request)
                 ├─ Router.resolve(path)         -> RoutingResult
                 │    └─ Route.match(path)       -> path_params
                 ├─ (OPTIONS? -> 204 + Allow)
                 ├─ pick handler by method
                 └─ Handler.handle(request)
                      └─ handler.get/post/...    -> Response
       ├─ Response
       └─ _safe_write(response)      # back in _handle_one
            └─ _write_response()
                 ├─ status line + headers (Server, Date, Connection, ...)
                 └─ _write_body()    -> Response.iter_body() chunks
```

Note:

- `dispatch` is the boundary for broadly catching exceptions:
  An `HTTPError` becomes a plain-text response and anything else is logged as 500.
  Either way the caller always gets a `Response`.
- The parser decides `keep_alive` up front; `_handle_one` returns whether to loop again.
- HEAD is routed to the `get` hook but the body is suppressed at write time.
  OPTIONS never reaches a handler: `App._handle` answers it with an `Allow` header built from the registered methods.

### Running the framework tests

Run discovery from the parent directory of the package:

```bash
python -m unittest discover webcan/tests -t ..
```

Or a subset as, e.g.,
```bash
python -m unittest webcan.tests.test_server_wire.TestBodyAndSmuggling
```

Or use Docker through the provided `Makefile` target `test`.

### Building

Run the build in Docker with
```bash
make build
```
