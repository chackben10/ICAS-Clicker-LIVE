from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from production_hub.integrations.scoreboard.repository import ScoreboardRepository
from production_hub.integrations.scoreboard.service import ScoreboardConflict, ScoreboardService


class ScoreboardTests(unittest.TestCase):
    def test_revision_updates_and_conflict_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = ScoreboardRepository(Path(temp), Path(temp) / "backups")
            service = ScoreboardService(repo)
            state = service.update_state({"rows": [{"id": "a", "name": "A", "score": 1}], "history": []})
            self.assertEqual(state.revision, 1)
            self.assertEqual(state.rows[0].score, 1)

            with self.assertRaises(ScoreboardConflict):
                service.update_state({"rows": [], "history": [], "expected_revision": 0}, expected_revision=0)

            next_state = service.update_state(
                {"rows": [{"id": "a", "name": "A", "score": 2}], "history": []},
                expected_revision=1,
            )
            self.assertEqual(next_state.revision, 2)
            self.assertEqual(next_state.rows[0].score, 2)


if __name__ == "__main__":
    unittest.main()

