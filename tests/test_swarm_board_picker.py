"""Pilot tests for the swarm-board room picker and its hand-off with the board.

Display and navigation run on an in-memory fakeredis. Archive, clear and the open-time sweep run Lua, so those use the
real local Redis (db 15 by default), like the other E2E tests. Run:  python -m unittest test_swarm_board_picker -v
"""
import os
import time
import unittest
import uuid
from unittest import mock
from urllib.parse import urlparse

try:
    import fakeredis
    import redis
    import textual  # noqa: F401
except ImportError:  # pragma: no cover - suite skips cleanly without the deps
    fakeredis = None

from test_swarm_board import ROOM, SIZE, board
from test_swarm_worker import URL, redis_up

HOUR_MS = 3_600_000


def seed(r, room, age_ms=0, count=2, title=None, present=0):
    base = int(time.time() * 1000) - age_ms
    for i in range(count):
        r.xadd(f"bus:stream:{room}", {"from": "a", "to": "all", "body": f"m{i}", "kind": "msg"}, id=f"{base + i}-0")
    if title:
        r.hset(f"bus:meta:{room}", "title", title)
    for i in range(present):
        r.set(f"bus:presence:{room}:w{i}", "now", ex=90)


def cell_text(app):
    table = app.query_one(board.DataTable)
    return [[cell.plain for cell in table.get_row_at(i)] for i in range(table.row_count)]


async def settle(pilot, predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out waiting")


@unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
class PickerDisplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.r = fakeredis.FakeStrictRedis(decode_responses=True)
        seed(self.r, "swarm-old", age_ms=30 * HOUR_MS, count=5, title="Epic 61")
        seed(self.r, "swarm-new", age_ms=60_000, count=3, title="Epic 62", present=2)

    def make_app(self):
        return board.RoomPicker(self.r, idle_hours=0, interval=0)

    async def load(self, app, pilot):
        app.apply(board.bus.list_rooms(self.r))
        await pilot.pause()

    async def test_lists_rooms_newest_first_with_title_messages_age_and_present(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertEqual(cell_text(app), [["swarm-new", "Epic 62", "3", "1m ago", "2"],
                                              ["swarm-old", "Epic 61", "5", "1d ago", "0"]])
            self.assertIn("2 rooms", app.query_one("#header").content.plain)

    async def test_enter_opens_the_selected_room(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("down", "enter")
        self.assertEqual(app.return_value, "swarm-old")

    async def test_refresh_picks_up_a_new_room_and_keeps_the_selection(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("down")
            seed(self.r, "swarm-newest", age_ms=1000)
            await pilot.press("r")
            await settle(pilot, lambda: len(app.rooms) == 3)
            await settle(pilot, lambda: app.selected_name() == "swarm-old")

    async def test_untrusted_title_is_shown_literally(self):
        self.r.hset("bus:meta:swarm-new", "title", "[bold red]x[/]\x1b[31m")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertIn("[bold red]x[/]", cell_text(app)[0][1])
            self.assertNotIn("\x1b", cell_text(app)[0][1])

    async def test_x_asks_first_and_any_other_key_cancels(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("x")
            self.assertIn("press y to confirm", app.query_one("#status").content.plain)
            await pilot.press("n")
            await pilot.pause()
            self.assertIsNone(app.pending)
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(self.r.exists("bus:stream:swarm-new", "bus:stream:swarm-old"), 2)

    async def test_board_b_goes_back_to_the_picker(self):
        for room in (ROOM, "swarm-new"):
            app = board.SwarmBoard(self.r, room, interval=0)
            async with app.run_test(size=SIZE) as pilot:
                await pilot.press("b")
            self.assertEqual(app.return_value, board.PICKER)

    def test_no_room_flag_means_picker_and_room_flag_means_board(self):
        self.assertIsNone(board.parse_args([]).room)
        self.assertEqual(board.parse_args(["--room", "x"]).room, "x")

    def test_browse_alternates_picker_and_board_until_quit(self):
        picker, boards = mock.Mock(), mock.Mock()
        picker.return_value.run.side_effect = ["a", "b", None]
        boards.return_value.run.side_effect = [board.PICKER, board.PICKER]
        with mock.patch.object(board, "RoomPicker", picker), mock.patch.object(board, "SwarmBoard", boards):
            board.browse(self.r, None)
        self.assertEqual([c.args[1] for c in boards.call_args_list], ["a", "b"])

    async def test_a_redis_error_in_a_write_shows_in_the_status_line_and_keeps_the_picker_up(self):
        app = self.make_app()
        with mock.patch.object(board.bus, "archive_room", side_effect=redis.ConnectionError("down")):
            async with app.run_test(size=SIZE) as pilot:
                await self.load(app, pilot)
                await pilot.press("a")
                await pilot.pause()
                self.assertIn("redis: down", app.query_one("#status").content.plain)
                await pilot.press("r")
                await pilot.pause()
                self.assertTrue(app.is_running)

    async def test_any_key_but_y_during_the_clear_prompt_only_cancels(self):
        app = self.make_app()
        with mock.patch.object(board.bus, "archive_room") as archive, mock.patch.object(board.bus, "clear_room") as clear:
            async with app.run_test(size=SIZE) as pilot:
                await self.load(app, pilot)
                for key in ("a", "escape", "r", "n"):
                    await pilot.press("x", key)
                    await pilot.pause()
                    self.assertIsNone(app.pending, key)
                    self.assertEqual(app.query_one("#status").content.plain, "cancelled", key)
        archive.assert_not_called()
        clear.assert_not_called()

    async def test_a_redis_error_clears_from_the_status_line_once_polling_recovers(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            app.apply_error("redis: blip")
            self.assertIn("redis: blip", app.query_one("#status").content.plain)
            await self.load(app, pilot)
            self.assertEqual(app.query_one("#status").content.plain, "")

    async def test_a_failed_first_poll_keeps_the_sweep_due_and_reports_rooms_archived_before_it(self):
        def archive_one_then_fail(r, idle_hours, retain_seconds, archived=None):
            archived.append({"room": "swarm-old", "archive_id": "swarm-old:1"})
            raise redis.ConnectionError("blip")

        app = board.RoomPicker(self.r, idle_hours=6, interval=0)
        with mock.patch.object(board.bus, "sweep_idle", side_effect=archive_one_then_fail):
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, lambda: app.error)
        self.assertTrue(app.sweep_due)
        self.assertIn("swarm-old", app.notes)

    async def test_long_multiline_titles_stay_one_bounded_line(self):
        self.r.hset("bus:meta:swarm-new", "title", "line1\nSECOND " + "very long " * 30)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            title = cell_text(app)[0][1]
            self.assertNotIn("\n", title)
            self.assertLessEqual(len(title), board.TITLE_WIDTH)
            self.assertEqual(cell_text(app)[0][2:], ["3", "1m ago", "2"])

    async def test_the_force_hint_names_the_url_unless_it_is_the_default(self):
        for url, expect in (("redis://h:6379/3", "bus --url redis://h:6379/3 clear --room swarm-new --force"),
                            (board.bus.DEFAULT_URL, "`bus clear --room swarm-new --force`")):
            app = board.RoomPicker(self.r, idle_hours=0, interval=0, url=url)
            async with app.run_test(size=SIZE) as pilot:
                await self.load(app, pilot)
                app.finish("swarm-new", {"status": "present", "present": 2}, "clear")
                self.assertIn(expect, app.query_one("#status").content.plain)

    def test_boards_do_not_read_the_archive_env_but_the_picker_does(self):
        picker, boards = mock.Mock(), mock.Mock()
        boards.return_value.run.return_value = None
        with mock.patch.dict(os.environ, {"BUS_ARCHIVE_IDLE_HOURS": "abc"}), \
                mock.patch.object(board, "RoomPicker", picker), mock.patch.object(board, "SwarmBoard", boards):
            board.browse(self.r, "x")
            with self.assertRaises(SystemExit):
                board.browse(self.r, None)
        picker.assert_not_called()

    def test_board_hint_with_the_rooms_key_is_no_longer_than_it_was(self):
        self.assertLessEqual(len(board.HINTS), 102)
        self.assertIn("b rooms", board.HINTS)

    def test_browse_with_a_room_opens_the_board_first_and_quits_without_b(self):
        picker, boards = mock.Mock(), mock.Mock()
        boards.return_value.run.return_value = None
        with mock.patch.object(board, "RoomPicker", picker), mock.patch.object(board, "SwarmBoard", boards):
            board.browse(self.r, "x")
        picker.assert_not_called()
        self.assertEqual(boards.call_args.args[1], "x")


@unittest.skipUnless(fakeredis is not None and redis_up(), "needs textual and a local Redis")
class PickerWritesE2E(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.r = redis.Redis.from_url(URL, decode_responses=True)
        # The sweep touches every idle room in its db, so its test gets a db of its own.
        self.sweep_r = redis.Redis.from_url(urlparse(URL)._replace(path="/14").geturl(), decode_responses=True)
        self.tag = "swt-" + uuid.uuid4().hex[:8]
        self.rooms = {name: f"{self.tag}-{name}" for name in ("idle", "busy", "fresh")}

    def tearDown(self):
        for client in (self.r, self.sweep_r):
            for key in client.scan_iter(match=f"bus:*:{self.tag}*"):
                client.delete(key)

    def make_app(self, idle_hours=0, r=None):
        return board.RoomPicker(r or self.r, idle_hours=idle_hours, interval=0)

    async def select(self, app, pilot, room):
        app.apply(board.bus.list_rooms(self.r))
        await pilot.pause()
        app.query_one(board.DataTable).move_cursor(row=[row["room"] for row in app.rooms].index(room))
        await pilot.pause()

    async def test_a_archives_the_selected_room(self):
        room = self.rooms["fresh"]
        seed(self.r, room)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.select(app, pilot, room)
            await pilot.press("a")
            await settle(pilot, lambda: room not in [row["room"] for row in app.rooms])
            self.assertIn("archived", app.query_one("#status").content.plain)
        self.assertEqual(self.r.exists(f"bus:stream:{room}"), 0)
        self.assertEqual(len(list(self.r.scan_iter(match=f"bus:archived:{room}:*"))), 1)

    async def test_a_and_x_refuse_a_room_with_members_present(self):
        room = self.rooms["busy"]
        seed(self.r, room, present=1)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.select(app, pilot, room)
            await pilot.press("a")
            await pilot.pause()
            self.assertIn("1 member(s) present", app.query_one("#status").content.plain)
            await pilot.press("x", "y")
            await pilot.pause()
            self.assertIn("1 member(s) present", app.query_one("#status").content.plain)
        self.assertEqual(self.r.exists(f"bus:stream:{room}"), 1)

    async def test_x_then_y_clears_every_key_of_the_room(self):
        room = self.rooms["fresh"]
        seed(self.r, room, title="t")
        self.r.set(f"bus:cursor:{room}:w1", "0-0")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.select(app, pilot, room)
            await pilot.press("x", "y")
            await settle(pilot, lambda: room not in [row["room"] for row in app.rooms])
        self.assertEqual(list(self.r.scan_iter(match=f"bus:*:{room}*")), [])

    async def test_opening_the_picker_sweeps_idle_rooms_but_never_present_ones(self):
        seed(self.sweep_r, self.rooms["idle"], age_ms=8 * HOUR_MS)
        seed(self.sweep_r, self.rooms["busy"], age_ms=8 * HOUR_MS, present=1)
        seed(self.sweep_r, self.rooms["fresh"])
        app = self.make_app(idle_hours=6, r=self.sweep_r)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, lambda: self.rooms["idle"] in app.notes)
            names = [row["room"] for row in app.rooms]
            self.assertNotIn(self.rooms["idle"], names)
            self.assertIn(self.rooms["busy"], names)
            self.assertIn(self.rooms["fresh"], names)
        self.assertEqual(self.sweep_r.exists(f"bus:stream:{self.rooms['idle']}"), 0)
        self.assertEqual(self.sweep_r.exists(f"bus:stream:{self.rooms['busy']}"), 1)


if __name__ == "__main__":
    unittest.main()
