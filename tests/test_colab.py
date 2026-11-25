import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from switchboard import colab  # noqa: E402

USAGE = "Current balance: 3026.50 compute units\nUsage rate: 30.15/hr\nActive assignments: 3\n"
SESS = ("[g4-db-cases] gpu-g4-s-asso1c1-36aep8jvwzsw6 | Hardware: G4 | Shape: Standard | Variant: GPU\n"
        "[g4-db-icl-worked] gpu-g4-s-euw4a4-3u27njg1mbg4p | Hardware: G4 | Shape: Standard | Variant: GPU\n")


class Colab(unittest.TestCase):
    def test_parse(self):
        r = colab.parse(USAGE, SESS)
        self.assertEqual((r["balance"], r["rate"], r["assignments"]), (3026.5, 30.15, 3))
        self.assertEqual(r["sessions"], [{"name": "g4-db-cases", "hardware": "G4"}, {"name": "g4-db-icl-worked", "hardware": "G4"}])
        self.assertEqual(r["runway_h"], 100.4)

    def test_idle_and_unreadable(self):
        r = colab.parse("Current balance: 1,200.00 compute units\nUsage rate: 0.00/hr\nActive assignments: 0\n")
        self.assertEqual((r["balance"], r["runway_h"], r["sessions"]), (1200.0, None, []))
        self.assertIsNone(colab.parse("Not signed in. Run colab login."))
        self.assertIsNone(colab.parse(""))

    def test_reading_is_cached_and_degrades(self):
        calls = []

        def run(*a):
            calls.append(a)
            return USAGE if a == ("usage",) else SESS
        colab._state.update(at=0.0, got=None, busy=False)
        tmp = tempfile.mkdtemp(prefix="sb-colab-")
        seen, log = os.path.join(tmp, "seen.json"), os.path.join(tmp, "none.log")
        r = colab.reading(now=1000.0, run=run, wait=True, seen=seen, log=log)
        self.assertEqual(r["balance"], 3026.5)
        colab.reading(run=run, wait=True, seen=seen, log=log)  # fresh: the CLI is not run again
        self.assertEqual(len(calls), 2)
        colab._state.update(at=0.0)
        self.assertIsNone(colab.reading(now=1000.0, run=lambda *a: "", wait=True, seen=seen, log=log))  # CLI gone or signed out

    def test_when_the_balance_last_changed(self):
        tmp = tempfile.mkdtemp(prefix="sb-colab-")
        seen, log = os.path.join(tmp, "seen.json"), os.path.join(tmp, "colab.log")
        with open(log, "w") as f:
            f.write('2026-11-22 04:52:55,100 - x - DEBUG - Response Body: {"currentBalance": 2941.80, "consumptionRateHourly": 10.05}\n'
                    '2026-11-22 04:58:27,100 - x - DEBUG - Response Body: {"currentBalance": 2940.9393, "consumptionRateHourly": 10.05}\n'
                    'a line with no timestamp\n'
                    '2026-11-22 08:53:41,100 - x - DEBUG - Response Body: {"currentBalance": 2940.9393, "consumptionRateHourly": 0}\n')
        first = time.mktime(time.strptime("2026-11-22 04:58:27", "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(colab.changed_at(2940.94, 5000.0, seen, log), first)   # nothing kept: from the CLI's log
        self.assertEqual(colab.changed_at(2940.94, 6000.0, seen, log), first)   # unchanged: the same moment
        self.assertEqual(colab.changed_at(2940.10, 7000.0, seen, log), 7000.0)  # it moved: now
        self.assertEqual(colab.changed_at(2940.10, 8000.0, seen, log), 7000.0)
        self.assertIsNone(colab.changed_at(10.0, 9000.0, os.path.join(tmp, "other.json"), log))  # a balance the log never held


if __name__ == "__main__":
    unittest.main()
