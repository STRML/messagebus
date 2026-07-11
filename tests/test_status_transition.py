"""Tests for the status-transition legality gate (bus `status_transition`).

Pure-function tests: no Redis, no gh, no network. The `bus` script has no .py
extension, so we load it as a module by path. Importing it runs its top-level
`import redis` but does NOT connect (redis-py connects lazily), so these run
against just the dependency being importable.

Run:  python -m unittest discover -s tests   (from the repo root)
"""
import importlib.util
import importlib.machinery
import os
import unittest

_BUS_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bus")


def _load_bus():
    # `bus` has no .py extension, so name a SourceFileLoader explicitly rather
    # than relying on extension-based loader inference (which yields spec=None).
    loader = importlib.machinery.SourceFileLoader("busmod", _BUS_PATH)
    spec = importlib.util.spec_from_loader("busmod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


bus = _load_bus()
T = bus.status_transition

OPEN = "status:open"
CLAIMED = "status:claimed"
PR = "status:pr-open"
MERGED = "status:merged"
DEPLOYED = "status:deployed"
VERIFIED = "status:verified"


class StatusTransition(unittest.TestCase):
    def test_no_prior_state_allows_any_set(self):
        for label in bus.STATUS_LABELS:
            self.assertEqual(T(None, label), "ok", label)

    def test_foreign_or_stale_current_label_allows_any_set(self):
        self.assertEqual(T("status:bogus", OPEN), "ok")
        self.assertEqual(T("needs-triage", MERGED), "ok")

    def test_self_loop_is_noop(self):
        for label in bus.STATUS_LABELS:
            self.assertEqual(T(label, label), "noop", label)

    def test_forward_by_one_is_ok(self):
        self.assertEqual(T(OPEN, CLAIMED), "ok")
        self.assertEqual(T(CLAIMED, PR), "ok")
        self.assertEqual(T(MERGED, DEPLOYED), "ok")
        self.assertEqual(T(DEPLOYED, VERIFIED), "ok")

    def test_forward_skip_is_ok(self):
        self.assertEqual(T(OPEN, VERIFIED), "ok")
        self.assertEqual(T(OPEN, PR), "ok")
        self.assertEqual(T(CLAIMED, MERGED), "ok")

    def test_allowed_back_edges(self):
        self.assertEqual(T(CLAIMED, OPEN), "ok")   # abandon a claim
        self.assertEqual(T(PR, CLAIMED), "ok")     # PR closed -> back to work

    def test_illegal_backward_moves(self):
        self.assertEqual(T(VERIFIED, OPEN), "illegal")
        self.assertEqual(T(MERGED, CLAIMED), "illegal")
        self.assertEqual(T(DEPLOYED, PR), "illegal")
        self.assertEqual(T(PR, OPEN), "illegal")   # not one of the two back-edges

    def test_unknown_target_label_raises(self):
        with self.assertRaises(ValueError):
            T(OPEN, "status:bogus")

    def test_legal_next_set_is_nonempty_except_terminal(self):
        # mirrors how cmd_status builds its "Legal next:" hint
        for current in bus.STATUS_LABELS[:-1]:
            legal = [l for l in bus.STATUS_LABELS if T(current, l) == "ok"]
            self.assertTrue(legal, f"{current} should have a legal forward move")
        # verified is terminal: no legal onward move
        self.assertEqual([l for l in bus.STATUS_LABELS if T(VERIFIED, l) == "ok"], [])


if __name__ == "__main__":
    unittest.main()
