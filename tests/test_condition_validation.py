"""Unit tests for the condition-validation invariant (spec: valid `condition`
values are exactly {1, 2, 3}). Exercises formation.creating_session directly
against a fake subsession/session, rather than a full oTree session, since
oTree's own session-creation path needs a configured project/DB that isn't
available here -- see build spec Section 15 on why live_method itself is
better tested via manual multi-tab devserver runs, not bots/unit tests.

Run with: python -m unittest discover -s tests
"""
import unittest
from unittest.mock import MagicMock

import formation


def _subsession_with_condition(condition):
    subsession = MagicMock()
    subsession.session.config = {'condition': condition}
    return subsession


class ConditionValidationTests(unittest.TestCase):
    def test_valid_conditions_are_accepted(self):
        for condition in (1, 2, 3):
            formation.creating_session(_subsession_with_condition(condition))

    def test_condition_4_is_rejected(self):
        with self.assertRaises(ValueError):
            formation.creating_session(_subsession_with_condition(4))

    def test_condition_0_is_rejected(self):
        with self.assertRaises(ValueError):
            formation.creating_session(_subsession_with_condition(0))

    def test_missing_condition_is_rejected(self):
        with self.assertRaises(ValueError):
            formation.creating_session(_subsession_with_condition(None))


if __name__ == '__main__':
    unittest.main()
