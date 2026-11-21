"""End-to-end: bus rooms / archive / unarchive / clear against a real local Redis (db 15 by default).

Failure matrix for archive and clear racing a concurrent `send` (every row is one Lua script, so a send lands wholly
before it or wholly after it):

  | interleaving                                  | outcome                                                  | test                          |
  |-----------------------------------------------|----------------------------------------------------------|-------------------------------|
  | send before the script                        | the message travels with the archive                     | test_concurrent_sends_*       |
  | send after the script                         | a fresh live stream; unarchive then refuses (name taken) | test_unarchive_refuses_*      |
  | member joins between presence check and move  | impossible: the check runs inside the script             | test_scripts_guard_presence   |
  | send between the idle check and the sweep     | impossible: the idle check runs inside the script        | test_scripts_guard_idleness   |
  | clear racing a send                           | all keys gone or a fresh stream, never a torn room       | test_concurrent_sends_*       |
"""
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import threading
import time
import unittest

from test_swarm_worker import BUS, URL, BusE2E, redis_up

DAY_MS = 86_400_000


def load_bus():
    loader = importlib.machinery.SourceFileLoader("bus_for_rooms_test", BUS)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@unittest.skipUnless(redis_up(), "local Redis not reachable")
class RoomsE2E(BusE2E):
    def setUp(self):
        super().setUp()
        self.env["BUS_ARCHIVE_IDLE_HOURS"] = "0"
        self.extra = []

    def tearDown(self):
        for room in self.extra:
            for key in self.r.scan_iter(match=f"bus:*:{room}*"):
                self.r.delete(key)
        super().tearDown()

    def cli(self, *args, **env):
        """Run bus WITHOUT the helper's global --room, so a missing --room is really missing (BUS_ROOM is still set)."""
        return subprocess.run([sys.executable, BUS, "--url", URL, *args], capture_output=True, text=True,
                              env=dict(self.env, **env))

    def other_room(self, suffix="b"):
        room = f"{self.room}-{suffix}"
        self.extra.append(room)
        return room

    def fill(self, room, count=3, age_ms=0, title=None):
        base = int(time.time() * 1000) - age_ms
        for i in range(count):
            self.r.xadd(f"bus:stream:{room}", {"from": "a", "to": "all", "body": f"m{i}", "kind": "msg"},
                        id=f"{base + i}-0")
        if title:
            self.r.hset(f"bus:meta:{room}", "title", title)

    def join(self, agent, room=None, present=True):
        room = room or self.room
        self.bus("--room", room, "join", "--as", agent, "--role", "worker", "--group", "api")
        if not present:
            self.r.delete(f"bus:presence:{room}:{agent}")

    def rooms(self, *args, **env):
        res = self.cli("rooms", "--json", *args, **env)
        self.assertEqual(res.returncode, 0, res.stderr)
        return {row.get("room"): row for row in json.loads(res.stdout)}

    def archive_keys(self, room=None):
        return sorted(self.r.scan_iter(match=f"bus:archived:{room or self.room}:*"))

    def live_keys(self, room=None):
        return sorted(self.r.scan_iter(match=f"bus:*:{room or self.room}:*")) + (
            [f"bus:stream:{room or self.room}"] if self.r.exists(f"bus:stream:{room or self.room}") else [])

    # ---- list ---------------------------------------------------------------------------------------------

    def test_rooms_lists_title_messages_activity_and_present(self):
        self.fill(self.room, count=3, age_ms=2 * 3_600_000, title="Epic 62")
        self.join("w1")
        row = self.rooms()[self.room]
        self.assertEqual((row["title"], row["messages"], row["present"]), ("Epic 62", 3, 1))
        self.assertAlmostEqual(row["last_activity_ms"], time.time() * 1000 - 2 * 3_600_000 + 2, delta=5000)
        self.assertRegex(row["last_activity"], r"^\d{4}-\d\d-\d\dT")

    def test_rooms_newest_activity_first_and_plain_output(self):
        newer, older = self.other_room("new"), self.other_room("old")
        self.fill(older, age_ms=5 * 3_600_000)
        self.fill(newer, age_ms=60_000)
        names = list(self.rooms())
        self.assertLess(names.index(newer), names.index(older))
        out = self.cli("rooms").stdout.splitlines()
        self.assertTrue(any(line.startswith(f"{newer}\t") for line in out))
        self.assertEqual(out[0], "ROOM\tTITLE\tMESSAGES\tLAST_ACTIVITY\tPRESENT")

    def test_archived_plain_output_has_a_header_and_an_expiry_date(self):
        self.fill(self.room)
        self.assertEqual(self.cli("--room", self.room, "archive").returncode, 0)
        out = self.cli("rooms", "--archived").stdout.splitlines()
        self.assertEqual(out[0], "ARCHIVE_ID\tTITLE\tMESSAGES\tARCHIVED\tEXPIRES")
        line = next(line for line in out if line.startswith(f"{self.room}:"))
        expires = line.split("\t")[4]
        self.assertRegex(expires, r"^\d{4}-\d\d-\d\dT")
        self.assertGreater(expires, line.split("\t")[3])

    # ---- archive / unarchive ------------------------------------------------------------------------------

    def test_archive_unarchive_round_trip_restores_everything_without_ttl(self):
        self.fill(self.room, count=3, title="Epic 62")
        self.join("w1", present=False)
        before = self.r.xrange(f"bus:stream:{self.room}")
        res = self.cli("--room", self.room, "archive", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        archive_id = json.loads(res.stdout)["archive_id"]
        self.assertTrue(archive_id.startswith(f"{self.room}:"))
        self.assertFalse(self.r.exists(f"bus:stream:{self.room}", f"bus:meta:{self.room}",
                                       f"bus:member:{self.room}:w1", f"bus:groups:{self.room}:w1"))
        self.assertEqual(self.r.exists(f"bus:cursor:{self.room}:w1"), 1)
        kept = self.archive_keys()
        self.assertEqual(len(kept), 4)
        for key in kept:
            self.assertTrue(key.startswith(f"bus:archived:{archive_id}:bus:"), key)
            self.assertTrue(0 < self.r.ttl(key) <= 7 * 86400, key)
        self.assertNotIn(self.room, self.rooms())
        listed = json.loads(self.cli("rooms", "--archived", "--json").stdout)
        entry = next(a for a in listed if a["archive_id"] == archive_id)
        self.assertEqual((entry["room"], entry["title"], entry["messages"]), (self.room, "Epic 62", 3))
        res = self.cli("unarchive", archive_id)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.r.xrange(f"bus:stream:{self.room}"), before)
        self.assertEqual(self.r.smembers(f"bus:groups:{self.room}:w1"), {"api"})
        self.assertEqual(self.r.hget(f"bus:meta:{self.room}", "title"), "Epic 62")
        self.assertEqual(self.archive_keys(), [])
        for key in (f"bus:stream:{self.room}", f"bus:meta:{self.room}", f"bus:member:{self.room}:w1"):
            self.assertEqual(self.r.ttl(key), -1, key)

    def test_retention_follows_env_days(self):
        self.fill(self.room)
        self.cli("--room", self.room, "archive", BUS_ARCHIVE_RETAIN_DAYS="2")
        ttl = self.r.ttl(self.archive_keys()[0])
        self.assertTrue(86400 < ttl <= 2 * 86400, ttl)

    def test_bad_retention_or_idle_env_is_an_error_before_anything_moves(self):
        self.fill(self.room, age_ms=48 * 3_600_000)
        for name, value in (("BUS_ARCHIVE_RETAIN_DAYS", "soon"), ("BUS_ARCHIVE_RETAIN_DAYS", "inf"),
                            ("BUS_ARCHIVE_RETAIN_DAYS", "1e15"), ("BUS_ARCHIVE_RETAIN_DAYS", "0"),
                            ("BUS_ARCHIVE_IDLE_HOURS", "nan"), ("BUS_ARCHIVE_IDLE_HOURS", "1e400")):
            for cmd in (("--room", self.room, "archive"), ("rooms",)):
                if name == "BUS_ARCHIVE_IDLE_HOURS" and cmd[-1] == "archive":
                    continue
                res = self.cli(*cmd, **{name: value})
                self.assertEqual(res.returncode, 2, (name, value, cmd, res.stderr))
                self.assertIn(name, res.stderr)
        self.assertEqual(self.cli("archive", "--idle-hours", "inf").returncode, 2)
        self.assertEqual(self.r.exists(f"bus:stream:{self.room}"), 1)

    def test_two_archives_in_the_same_millisecond_never_overwrite_each_other(self):
        mod = load_bus()
        self.fill(self.room, count=2)
        now = int(time.time() * 1000)
        taken = [f"bus:archived:{self.room}:{ms}:bus:stream:{self.room}" for ms in range(now - 50, now + 400)]
        self.r.mset({key: "first archive" for key in taken})
        result = mod.archive_room(self.r, self.room, 60)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn(f"bus:archived:{result['archive_id']}:bus:stream:{self.room}", taken)
        self.assertEqual({self.r.get(key) for key in taken}, {"first archive"})
        self.assertEqual(self.r.xlen(f"bus:archived:{result['archive_id']}:bus:stream:{self.room}"), 2)

    def test_archive_refuses_while_a_member_is_present_and_force_overrides(self):
        self.fill(self.room)
        self.join("w1")
        res = self.cli("--room", self.room, "archive")
        self.assertEqual(res.returncode, 1)
        self.assertIn("REFUSING", res.stderr)
        self.assertEqual(self.archive_keys(), [])
        self.assertEqual(self.r.exists(f"bus:stream:{self.room}"), 1)
        self.assertEqual(self.cli("--room", self.room, "archive", "--force").returncode, 0)
        self.assertEqual(self.r.exists(f"bus:stream:{self.room}"), 0)
        self.assertTrue(self.archive_keys())

    def test_cursor_survives_archive_of_its_room(self):
        self.join("w1")
        self.bus("--room", self.room, "send", "--from", "coord", "--to", "w1", "before")
        self.assertIn("before", self.bus("--room", self.room, "poll", "--as", "w1").stdout)
        cursor = self.r.get(f"bus:cursor:{self.room}:w1")
        self.cli("--room", self.room, "archive", "--force")
        self.assertEqual(self.r.get(f"bus:cursor:{self.room}:w1"), cursor)
        time.sleep(0.01)
        self.bus("--room", self.room, "send", "--from", "coord", "--to", "w1", "after")
        out = self.bus("--room", self.room, "poll", "--as", "w1").stdout
        self.assertIn("after", out)
        self.assertNotIn("before", out)

    def test_idle_sweep_archives_idle_rooms_and_skips_present_ones(self):
        idle, busy, fresh = self.other_room("idle"), self.other_room("busy"), self.other_room("fresh")
        for room, age in ((idle, 8), (busy, 8), (fresh, 0)):
            self.fill(room, age_ms=age * 3_600_000)
        self.join("w1", room=busy)
        res = self.cli("archive", "--idle-hours", "6")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.r.exists(f"bus:stream:{idle}"), 0)
        self.assertTrue(self.archive_keys(idle))
        self.assertEqual(self.r.exists(f"bus:stream:{busy}", f"bus:stream:{fresh}"), 2)

    def test_idle_hours_zero_disables_the_sweep(self):
        self.fill(self.room, age_ms=48 * 3_600_000)
        self.assertEqual(self.cli("archive", "--idle-hours", "0").returncode, 0)
        self.assertEqual(self.cli("rooms", BUS_ARCHIVE_IDLE_HOURS="0").returncode, 0)
        self.assertEqual(self.r.exists(f"bus:stream:{self.room}"), 1)

    def test_rooms_runs_the_sweep_with_notice_on_stderr_and_clean_json(self):
        self.fill(self.room, age_ms=8 * 3_600_000)
        res = self.cli("rooms", "--json", BUS_ARCHIVE_IDLE_HOURS="6")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(self.room, res.stderr)
        self.assertNotIn(self.room, [row["room"] for row in json.loads(res.stdout)])
        self.assertTrue(self.archive_keys())

    def test_unarchive_refuses_when_the_live_name_is_taken(self):
        self.fill(self.room, count=2)
        archive_id = json.loads(self.cli("--room", self.room, "archive", "--json").stdout)["archive_id"]
        self.bus("--room", self.room, "send", "--from", "coord", "after the archive")
        res = self.cli("unarchive", archive_id)
        self.assertEqual(res.returncode, 1)
        self.assertIn("REFUSING", res.stderr)
        self.assertEqual(self.r.xlen(f"bus:stream:{self.room}"), 1)
        self.assertTrue(self.archive_keys())

    def test_unarchive_unknown_or_malformed_id(self):
        self.assertEqual(self.cli("unarchive", f"{self.room}:123").returncode, 1)
        self.assertEqual(self.cli("unarchive", "no*glob:1").returncode, 2)

    def test_archive_and_clear_leave_prefix_sibling_rooms_alone(self):
        sibling = self.other_room("b")
        for room in (self.room, sibling):
            self.fill(room)
            self.join("w1", room=room, present=False)
        self.cli("--room", self.room, "archive")
        self.assertEqual(len(self.live_keys(sibling)), 4)
        self.cli("--room", self.room, "unarchive", self.archive_id_of(self.room))
        self.cli("--room", self.room, "clear")
        self.assertEqual(self.live_keys(), [])
        self.assertEqual(len(self.live_keys(sibling)), 4)

    def archive_id_of(self, room):
        return self.archive_keys(room)[0].split(":bus:", 1)[0].removeprefix("bus:archived:")

    # ---- clear --------------------------------------------------------------------------------------------

    def test_clear_deletes_every_key_including_cursors_and_presence_with_force(self):
        self.fill(self.room, title="Epic 62")
        self.join("w1")
        res = self.cli("--room", self.room, "clear")
        self.assertEqual(res.returncode, 1)
        self.assertIn("REFUSING", res.stderr)
        self.assertTrue(self.live_keys())
        self.assertEqual(self.cli("--room", self.room, "clear", "--force").returncode, 0)
        self.assertEqual(self.live_keys(), [])
        self.assertEqual(self.r.exists(f"bus:meta:{self.room}"), 0)
        self.assertEqual(self.cli("--room", self.room, "clear").returncode, 1)

    def test_clear_after_presence_expired_needs_no_force(self):
        self.fill(self.room)
        self.join("w1", present=False)
        self.assertEqual(self.cli("--room", self.room, "clear").returncode, 0)
        self.assertEqual(self.live_keys(), [])

    # ---- argument handling --------------------------------------------------------------------------------

    def test_archive_and_clear_never_fall_back_to_the_default_room(self):
        self.fill(self.room)
        for cmd in ("archive", "clear"):
            res = self.cli(cmd, "--force")
            self.assertEqual(res.returncode, 2, cmd)
            self.assertIn("--room", res.stderr)
        self.assertEqual(self.r.exists(f"bus:stream:{self.room}"), 1)

    def test_room_flag_works_before_or_after_the_subcommand(self):
        self.fill(self.room)
        self.assertEqual(self.bus("archive", "--room", self.room).returncode, 0)
        self.assertEqual(self.r.exists(f"bus:stream:{self.room}"), 0)

    def test_room_and_idle_hours_together_are_rejected(self):
        self.fill(self.room, age_ms=8 * 3_600_000)
        res = self.cli("--room", self.room, "archive", "--idle-hours", "6")
        self.assertEqual(res.returncode, 2)
        self.assertEqual(self.r.exists(f"bus:stream:{self.room}"), 1)

    # ---- atomicity ----------------------------------------------------------------------------------------

    def test_scripts_guard_presence_and_idleness_themselves(self):
        mod = load_bus()
        self.fill(self.room)
        self.join("w1")
        self.assertEqual(mod.archive_room(self.r, self.room, 60, force=False)["status"], "present")
        self.r.delete(f"bus:presence:{self.room}:w1")
        self.assertEqual(mod.archive_room(self.r, self.room, 60, idle_ms=DAY_MS)["status"], "active")
        self.assertEqual(self.r.exists(f"bus:stream:{self.room}"), 1)
        self.assertEqual(mod.archive_room(self.r, self.room, 60, idle_ms=0)["status"], "ok")

    def test_concurrent_sends_land_wholly_before_or_after_archive_and_clear(self):
        mod = load_bus()
        for op in ("archive", "clear"):
            room = self.other_room(op)
            for i in range(300):
                self.r.hset(f"bus:member:{room}:w{i}", mapping={"role": "worker"})
            self.fill(room, count=1)
            stop, sent = threading.Event(), []

            def hammer():
                writer = mod.connect(URL)
                while not stop.is_set():
                    sent.append(writer.xadd(f"bus:stream:{room}", {"from": "a", "to": "all", "body": "x", "kind": "msg"}))

            worker = threading.Thread(target=hammer, daemon=True)
            self.addCleanup(stop.set)
            worker.start()
            time.sleep(0.05)
            if op == "archive":
                result = mod.archive_room(self.r, room, 60, force=True)
                self.assertEqual((result["status"], result["keys"]), ("ok", 301))
            else:
                result = mod.clear_room(self.r, room, force=True)
                self.assertEqual(result["status"], "ok")
            stop.set()
            worker.join()
            live = self.r.xlen(f"bus:stream:{room}")
            self.assertEqual(list(self.r.scan_iter(match=f"bus:member:{room}:*")), [])
            if op == "archive":
                archived = self.r.xlen(f"bus:archived:{result['archive_id']}:bus:stream:{room}")
                self.assertEqual(archived + live, len(sent) + 1)
                self.assertEqual(len(list(self.r.scan_iter(match=f"bus:archived:{result['archive_id']}:bus:member:*"))), 300)
            else:
                self.assertLessEqual(live, len(sent))


if __name__ == "__main__":
    unittest.main()
