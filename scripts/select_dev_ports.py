"""Select distinct, available ports for one local development session."""

from __future__ import annotations

import argparse
import socket
from contextlib import ExitStack


def reserve_port(host: str, preferred_port: int) -> socket.socket:
    """Bind the preferred port, or let the OS select a free replacement."""
    if not 1 <= preferred_port <= 65535:
        raise ValueError(f"port must be between 1 and 65535: {preferred_port}")

    last_error: OSError | None = None
    for port in (preferred_port, 0):
        for family, socktype, proto, _, address in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        ):
            reservation = socket.socket(family, socktype, proto)
            try:
                reservation.bind(address)
            except OSError as error:
                last_error = error
                reservation.close()
                continue
            return reservation

    assert last_error is not None
    raise last_error


def select_ports(
    api_host: str,
    api_preferred_port: int,
    frontend_host: str,
    frontend_preferred_port: int,
) -> tuple[int, int]:
    """Reserve both ports at once so the assignments cannot collide."""
    with ExitStack() as reservations:
        api_socket = reservations.enter_context(
            reserve_port(api_host, api_preferred_port)
        )
        frontend_socket = reservations.enter_context(
            reserve_port(frontend_host, frontend_preferred_port)
        )
        return api_socket.getsockname()[1], frontend_socket.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--frontend-host", default="127.0.0.1")
    parser.add_argument("--frontend-port", type=int, default=5173)
    args = parser.parse_args()

    api_port, frontend_port = select_ports(
        args.api_host,
        args.api_port,
        args.frontend_host,
        args.frontend_port,
    )
    print(api_port, frontend_port)


if __name__ == "__main__":
    main()
