"""Admin → Logs: what is listed and what clearing removes (a temporary copy of the checkout's layout)."""

import json
import tempfile
import time
import unittest
from pathlib import Path

import logs
import sessions


def stamp(t):
    return time.strftime("%d/%b/%Y:%H:%M:%S %z", time.localtime(t))


class LogsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = self.repo = Path(self.tmp.name)
        (repo / "gateway" / "logs").mkdir(parents=True)
        now = time.time()
        old = '10.0.0.1 - - [{}] "GET /old HTTP/1.1" 200 5 "-" "x"\n'.format(stamp(now - 30 * 86400))
        new = '10.0.0.1 - - [{}] "GET /new HTTP/1.1" 200 5 "-" "x"\n'.format(stamp(now - 3600))
        (repo / "gateway" / "logs" / "access.log").write_text(old * 3 + new * 2)
        (repo / "gateway" / "logs" / "error.log").write_text("apache\n")
        (repo / "state" / "jobs").mkdir(parents=True)
        (repo / "state" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "run1", "state": "running"}, {"id": "done1", "state": "done"}]}))
        for jid in ("run1", "done1", "gone1"):
            (repo / "state" / "jobs" / (jid + ".log")).write_text("log of " + jid)
        (repo / "state" / "workbench" / "jobs").mkdir(parents=True)
        for jid, state in (("aaaaaaaaaaaa", "running"), ("bbbbbbbbbbbb", "done")):
            (repo / "state" / "workbench" / "runs" / jid).mkdir(parents=True)
            (repo / "state" / "workbench" / "runs" / jid / "log").write_text("x" * 100)
            (repo / "state" / "workbench" / "jobs" / (jid + ".json")).write_text(json.dumps({"id": jid, "state": state}))
        self.store = sessions.Store(repo / "state" / "auth")
        self.store.log("10.0.0.1", "alice", "account", True)
        self.logs = logs.Logs(self.store, repo)

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, name):
        return {i["id"]: i for i in self.logs.inventory()}[name]

    def test_inventory(self):
        items = {i["id"]: i for i in self.logs.inventory()}
        self.assertEqual(set(items), {"access", "gateway-old", "signin", "conversions", "downloads", "journal"})
        self.assertGreater(items["access"]["size"], 0)
        self.assertLess(items["access"]["oldest"], time.time() - 29 * 86400)
        self.assertEqual(items["signin"]["entries"], 1)
        self.assertEqual(items["conversions"]["entries"], 2)  # not the running one
        self.assertEqual(items["downloads"]["entries"], 1)
        self.assertEqual(items["journal"]["actions"], [])  # root's: only the command is shown

    def test_access_log(self):
        access = self.repo / "gateway" / "logs" / "access.log"
        self.assertGreater(self.logs.clean("access", "keep7")["freed"], 0)
        text = access.read_text()
        self.assertNotIn("/old", text)
        self.assertEqual(text.count("/new"), 2)
        self.assertEqual(self.logs.clean("access", "keep7")["freed"], 0)  # nothing older is left
        with open(str(access), "a", buffering=1) as writer:  # as the gateway: open for appending all along
            self.logs.clean("access", "clear")
            writer.write("after\n")
        self.assertEqual(access.read_text(), "after\n")

    def test_others(self):
        self.logs.clean("gateway-old", "delete")
        self.assertFalse((self.repo / "gateway" / "logs" / "error.log").exists())
        self.assertTrue((self.repo / "gateway" / "logs" / "access.log").exists())
        self.assertEqual(self.logs.clean("signin", "keep30")["removed"], 0)
        self.assertEqual(self.logs.clean("signin", "clear")["removed"], 1)
        self.assertEqual(self.logs.clean("conversions", "clear")["removed"], 2)
        self.assertEqual(sorted(p.name for p in (self.repo / "state" / "jobs").iterdir()), ["run1.log"])
        self.assertEqual(self.logs.clean("downloads", "clear")["removed"], 1)
        self.assertEqual([p.name for p in (self.repo / "state" / "workbench" / "runs").iterdir()], ["aaaaaaaaaaaa"])
        with self.assertRaises(KeyError):
            self.logs.clean("journal", "clear")
        with self.assertRaises(KeyError):
            self.logs.clean("../state", "clear")


if __name__ == "__main__":
    unittest.main()
