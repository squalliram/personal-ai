"""Threaded local server. Loopback by default; any other bind address requires TLS."""

from __future__ import annotations

import ipaddress
import ssl
import sys
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import ServerHandler, WSGIRequestHandler, WSGIServer, make_server

from .app import CommandCentreApp
from .config import Settings


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _ServerHandler(ServerHandler):
    server_software = "personal-ai"


class Handler(WSGIRequestHandler):
    server_version = "personal-ai"
    sys_version = ""

    def handle(self) -> None:
        # Mirrors WSGIRequestHandler.handle so the Server header omits the Python version.
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline = self.request_version = self.command = ""
            self.send_error(414)
            return
        if not self.parse_request():
            return
        handler = _ServerHandler(self.rfile, self.wfile, self.get_stderr(), self.get_environ(), multithread=False)
        handler.request_handler = self
        handler.run(self.server.get_app())

    def log_message(self, format: str, *args) -> None:
        # Request lines carry no secrets: there are no query-string credentials and cookies are not logged.
        sys.stderr.write("command-centre " + (format % args) + "\n")


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build(
    settings: Settings,
    host: str,
    port: int,
    *,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    extra_hosts: tuple[str, ...] = (),
) -> tuple[WSGIServer, CommandCentreApp]:
    secure = tls_cert is not None
    if (tls_cert is None) != (tls_key is None):
        raise ValueError("Provide both --tls-cert and --tls-key")
    if not is_loopback(host) and not secure:
        raise ValueError("Refusing a non-loopback bind without TLS")
    if not settings.password_hash:
        raise ValueError("COMMAND_CENTRE_PASSWORD_HASH is not configured; run: python -m command_centre hash-password")
    holder: dict[str, CommandCentreApp] = {}

    def application(environ, start_response):
        return holder["app"](environ, start_response)

    server = make_server(host, port, application, server_class=ThreadingWSGIServer, handler_class=Handler)
    bound = server.server_address[1]
    names = {"127.0.0.1", "localhost", "[::1]"} if is_loopback(host) else {host}
    allowed = {f"{name}:{bound}" for name in names} | set(extra_hosts)
    holder["app"] = CommandCentreApp(settings, allowed_hosts=allowed, secure=secure)
    if secure:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(tls_cert, tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server, holder["app"]
