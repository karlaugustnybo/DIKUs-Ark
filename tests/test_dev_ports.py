from __future__ import annotations

import socket
import unittest

from scripts.select_dev_ports import select_ports


class DevelopmentPortTests(unittest.TestCase):
    def test_keeps_available_preferred_ports(self) -> None:
        first = socket.socket()
        second = socket.socket()
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        api_preferred = first.getsockname()[1]
        frontend_preferred = second.getsockname()[1]
        first.close()
        second.close()

        self.assertEqual(
            select_ports(
                "127.0.0.1", api_preferred, "127.0.0.1", frontend_preferred
            ),
            (api_preferred, frontend_preferred),
        )

    def test_replaces_an_occupied_preferred_port(self) -> None:
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        preferred = occupied.getsockname()[1]

        api_port, frontend_port = select_ports(
            "127.0.0.1", preferred, "127.0.0.1", preferred
        )

        self.assertNotEqual(api_port, preferred)
        self.assertNotEqual(frontend_port, preferred)
        self.assertNotEqual(api_port, frontend_port)

    def test_prevents_services_with_the_same_preference_from_colliding(self) -> None:
        probe = socket.socket()
        self.addCleanup(probe.close)
        probe.bind(("127.0.0.1", 0))
        preferred = probe.getsockname()[1]
        probe.close()

        api_port, frontend_port = select_ports(
            "127.0.0.1", preferred, "127.0.0.1", preferred
        )

        self.assertEqual(api_port, preferred)
        self.assertNotEqual(frontend_port, preferred)


if __name__ == "__main__":
    unittest.main()
