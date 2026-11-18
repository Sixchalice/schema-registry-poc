"""Local TCP servers for tests that need a real socket (no Docker)."""
from __future__ import annotations

import socket
import threading
from collections.abc import Callable


class LocalServer:
    """A server on 127.0.0.1 that runs `handle(conn, server)` for each connection, each in its own thread.

    Use it as a context manager: leaving it stops accepting, sets `stopping` (handlers
    that loop must check it), waits for the handlers and closes the listener.
    """

    def __init__(self, handle: Callable[[socket.socket, LocalServer], None]) -> None:
        self._handle = handle
        self.stopping = threading.Event()
        # Set when a handler sees the client close its side (EOF or a failed send).
        self.peer_closed = threading.Event()
        self.connections = 0
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.05)
        self._threads: list[threading.Thread] = []
        self._acceptor = threading.Thread(target=self._accept, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

    def __enter__(self) -> LocalServer:
        self._acceptor.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stopping.set()
        self._acceptor.join()
        for thread in self._threads:
            thread.join(5)
        self._listener.close()

    def _accept(self) -> None:
        while not self.stopping.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            self.connections += 1
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            try:
                self._handle(conn, self)
            except OSError:
                self.peer_closed.set()


def read_request(conn: socket.socket, server: LocalServer) -> bytes:
    """Read up to the end of one request's headers; b"" if the client closed or the server stops first."""
    data = b""
    conn.settimeout(0.05)
    try:
        while b"\r\n\r\n" not in data:
            try:
                piece = conn.recv(65536)
            except TimeoutError:
                if server.stopping.is_set():
                    return b""
                continue
            if not piece:
                return b""
            data += piece
        return data
    finally:
        conn.settimeout(None)


def send_drip(conn: socket.socket, server: LocalServer, prefix: bytes, gap: float = 0.2) -> None:
    """Send `prefix`, then one more byte every `gap` seconds until the client closes."""
    conn.sendall(prefix)
    conn.settimeout(gap)
    while not server.stopping.is_set():
        try:
            if conn.recv(65536) == b"":
                server.peer_closed.set()
                return
        except TimeoutError:
            conn.sendall(b"a")


def drip(prefix: bytes, gap: float = 0.2) -> Callable[[socket.socket, LocalServer], None]:
    """A handler that reads the request and then drips the response (send_drip)."""

    def handle(conn: socket.socket, server: LocalServer) -> None:
        read_request(conn, server)
        send_drip(conn, server, prefix, gap)

    return handle


def respond(status: str, body: bytes, headers: str = "") -> Callable[[socket.socket, LocalServer], None]:
    """A handler that answers every request on the connection with this response (keep-alive)."""
    head = (f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n{headers}\r\n").encode()

    def handle(conn: socket.socket, server: LocalServer) -> None:
        while read_request(conn, server):
            conn.sendall(head + body)

    return handle
