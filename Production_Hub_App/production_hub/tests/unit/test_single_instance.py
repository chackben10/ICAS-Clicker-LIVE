from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from production_hub.app.single_instance import SingleInstanceLock


class SingleInstanceLockTests(unittest.TestCase):
    def test_second_process_lock_is_rejected_until_first_releases(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "production-hub.lock"
            first = SingleInstanceLock.acquire(path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(SingleInstanceLock.acquire(path))
            finally:
                assert first is not None
                first.release()

            replacement = SingleInstanceLock.acquire(path)
            self.assertIsNotNone(replacement)
            assert replacement is not None
            replacement.release()


if __name__ == "__main__":
    unittest.main()
