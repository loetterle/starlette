import http.cookies
import json
import typing
from collections.abc import Mapping

from starlette.datastructures import URL, Address, Headers, QueryParams
from starlette.formparsers import FormParser, MultiPartParser
from starlette.types import Message, Receive, Scope

try:
    from multipart.multipart import parse_options_header
except ImportError:  # pragma: nocover
    parse_options_header = None  # type: ignore


class ClientDisconnect(Exception):
    pass


class HTTPConnection(Mapping):
    """
    A base class for incoming HTTP connections, that is used to provide
    any functionality that is common to both `Request` and `WebSocket`.
    """

    def __init__(self, scope: Scope, receive: Receive = None) -> None:
        assert scope["type"] in ("http", "websocket")
        self._scope = scope

    def __getitem__(self, key: str) -> str:
        return self._scope[key]

    def __iter__(self) -> typing.Iterator[str]:
        return iter(self._scope)

    def __len__(self) -> int:
        return len(self._scope)

    @property
    def url(self) -> URL:
        if not hasattr(self, "_url"):
            self._url = URL(scope=self._scope)
        return self._url

    @property
    def headers(self) -> Headers:
        if not hasattr(self, "_headers"):
            self._headers = Headers(scope=self._scope)
        return self._headers

    @property
    def query_params(self) -> QueryParams:
        if not hasattr(self, "_query_params"):
            self._query_params = QueryParams(scope=self._scope)
        return self._query_params

    @property
    def path_params(self) -> dict:
        return self._scope.get("path_params", {})

    @property
    def cookies(self) -> typing.Dict[str, str]:
        if not hasattr(self, "_cookies"):
            cookies = {}
            cookie_header = self.headers.get("cookie")
            if cookie_header:
                cookie = http.cookies.SimpleCookie()
                cookie.load(cookie_header)
                for key, morsel in cookie.items():
                    cookies[key] = morsel.value
            self._cookies = cookies
        return self._cookies

    @property
    def client(self) -> Address:
        host, port = self._scope.get("client") or (None, None)
        return Address(host=host, port=port)

    @property
    def session(self) -> dict:
        assert (
            "session" in self._scope
        ), "SessionMiddleware must be installed to access request.session"
        return self._scope["session"]

    @property
    def database(self) -> typing.Any:
        assert (
            "database" in self._scope
        ), "DatabaseMiddleware must be installed to access request.database"
        return self._scope["database"]

    @property
    def auth(self) -> typing.Any:
        assert (
            "auth" in self._scope
        ), "AuthenticationMiddleware must be installed to access request.auth"
        return self._scope["auth"]

    @property
    def user(self) -> typing.Any:
        assert (
            "user" in self._scope
        ), "AuthenticationMiddleware must be installed to access request.user"
        return self._scope["user"]

    def url_for(self, name: str, **path_params: typing.Any) -> str:
        router = self._scope["router"]
        url_path = router.url_path_for(name, **path_params)
        return url_path.make_absolute_url(base_url=self.url)


async def empty_receive() -> Message:
    raise RuntimeError("Receive channel has not been made available")


class Request(HTTPConnection):
    def __init__(self, scope: Scope, receive: Receive = empty_receive):
        super().__init__(scope)
        assert scope["type"] == "http"
        self._receive = receive
        self._stream_consumed = False

    @property
    def method(self) -> str:
        return self._scope["method"]

    @property
    def receive(self) -> Receive:
        return self._receive

    async def stream(self) -> typing.AsyncGenerator[bytes, None]:
        if hasattr(self, "_body"):
            yield self._body
            yield b""
            return

        if self._stream_consumed:
            raise RuntimeError("Stream consumed")

        self._stream_consumed = True
        while True:
            message = await self._receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if body:
                    yield body
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                raise ClientDisconnect()
        yield b""

    async def body(self) -> bytes:
        if not hasattr(self, "_body"):
            body = b""
            async for chunk in self.stream():
                body += chunk
            self._body = body
        return self._body

    async def json(self) -> typing.Any:
        if not hasattr(self, "_json"):
            body = await self.body()
            self._json = json.loads(body)
        return self._json

    async def form(self) -> dict:
        if not hasattr(self, "_form"):
            assert (
                parse_options_header is not None
            ), "The `python-multipart` library must be installed to use form parsing."
            content_type_header = self.headers.get("Content-Type")
            content_type, options = parse_options_header(content_type_header)
            if content_type == b"multipart/form-data":
                multipart_parser = MultiPartParser(self.headers, self.stream())
                self._form = await multipart_parser.parse()
            elif content_type == b"application/x-www-form-urlencoded":
                form_parser = FormParser(self.headers, self.stream())
                self._form = await form_parser.parse()
            else:
                self._form = {}
        return self._form

    async def close(self) -> None:
        if hasattr(self, "_form"):
            for item in self._form.values():
                if hasattr(item, "close"):
                    await item.close()  # type: ignore
