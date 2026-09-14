import asyncio
import signal
import unittest
from unittest.mock import AsyncMock, patch

import main


class GracefulShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_path_awaits_service_stop_once(self):
        started = asyncio.Event()

        class FakeService:
            def __init__(self):
                self.stop = AsyncMock()

            async def start(self):
                started.set()
                await asyncio.Event().wait()

        service = FakeService()
        callbacks = {}
        loop = asyncio.get_running_loop()

        def add_handler(signum, callback):
            callbacks[signum] = callback

        with (
            patch.object(main, "Service", return_value=service),
            patch.object(loop, "add_signal_handler", side_effect=add_handler),
            patch.object(loop, "remove_signal_handler", return_value=True),
        ):
            task = asyncio.create_task(main.main())
            await started.wait()
            callbacks[signal.SIGTERM]()
            self.assertEqual(await task, 0)

        service.stop.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
