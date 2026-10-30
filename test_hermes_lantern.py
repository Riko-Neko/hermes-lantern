import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import hermes_lantern as hw


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.db"
        self.writer = sqlite3.connect(self.db, isolation_level=None)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.executescript("""
            CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,source TEXT,started_at REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,
                                  tool_name TEXT,tool_calls TEXT,timestamp REAL,reasoning TEXT);
            CREATE INDEX session_messages ON messages(session_id,id);
            INSERT INTO sessions VALUES('one','测试会话','cli',1);
        """)
        self.store = hw.Store(self.db)

    def tearDown(self):
        self.store.close()
        self.writer.close()
        self.tmp.cleanup()

    def args(self, *options):
        a = hw.parser().parse_args(options)
        a.session = a.session or a.session_id
        return a

    def add(self, role, content, sid="one", calls=None, tool=None):
        return self.writer.execute(
            "INSERT INTO messages(session_id,role,content,tool_name,tool_calls,timestamp,reasoning) VALUES(?,?,?,?,?,?,?)",
            (sid, role, content, tool, calls, time.time(), "HIDDEN_REASONING"),
        ).lastrowid

    def texts(self, events):
        return [e["content"] for e in events if e["event"] == "message"]

    def test_append_update_and_no_duplicates_with_wal_writer(self):
        mid = self.add("user", "你好")
        watcher = hw.Watcher(self.store, self.args())
        self.assertEqual(self.texts(watcher.poll()), ["你好"])
        self.assertEqual(watcher.poll(), [])
        self.add("assistant", "新回复")
        self.assertEqual(self.texts(watcher.poll()), ["新回复"])
        self.writer.execute("UPDATE messages SET content=? WHERE id=?", ("你好，更新", mid))
        update = watcher.poll()
        self.assertEqual(self.texts(update), ["你好，更新"])
        self.assertTrue(update[0]["updated"])

    def test_automatic_activity_switch_and_fixed_session(self):
        self.add("user", "one")
        auto = hw.Watcher(self.store, self.args())
        fixed = hw.Watcher(self.store, self.args("one"))
        auto.poll()
        fixed.poll()
        self.writer.execute("INSERT INTO sessions VALUES('two','second','qqbot',2)")
        self.add("assistant", "two", "two")
        self.assertEqual(auto.poll()[0]["id"], "two")
        self.assertEqual(fixed.poll(), [])
        self.add("user", "back", "one")
        returned = auto.poll()
        self.assertEqual(returned[0]["id"], "one")
        self.assertEqual(self.texts(returned), ["back"])

    def test_chat_only_does_not_read_system_or_reasoning(self):
        self.add("system", "HIDDEN_SYSTEM")
        self.add("assistant", "", calls='[{"function":{"name":"terminal","arguments":"pwd"}}]')
        self.add("tool", '{"output":"tool result","exit_code":0}', tool="terminal")
        self.add("assistant", "正文")
        events = hw.Watcher(self.store, self.args()).poll()
        self.assertEqual(self.texts(events), ["正文"])
        self.assertNotIn("HIDDEN_", json.dumps(events))
        tools = hw.Watcher(self.store, self.args("--tools")).poll()
        self.assertEqual(len(self.texts(tools)), 3)
        self.assertNotIn("HIDDEN_", json.dumps(tools))

    def test_tail_source_and_zero_history(self):
        for i in range(5):
            self.add("user", str(i))
        self.assertEqual(self.texts(hw.Watcher(self.store, self.args("--tail", "2")).poll()), ["3", "4"])
        watcher = hw.Watcher(self.store, self.args("--tail", "0"))
        self.assertEqual(self.texts(watcher.poll()), [])
        self.add("assistant", "after")
        self.assertEqual(self.texts(watcher.poll()), ["after"])
        self.writer.execute("INSERT INTO sessions VALUES('qq','qq','qqbot',2)")
        self.add("user", "qq only", "qq")
        cli = hw.Watcher(self.store, self.args("--source", "cli"))
        self.assertEqual(cli.poll()[0]["id"], "one")

    def test_read_only_and_missing_db_never_created(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.store.conn.execute("DELETE FROM messages")
        missing = Path(self.tmp.name) / "missing.db"
        with self.assertRaises(ValueError):
            hw.Store(missing)
        self.assertFalse(missing.exists())

    def test_sql_parameters_and_terminal_control_sequences(self):
        with self.assertRaises(ValueError):
            self.store.session("one' OR 1=1--")
        self.assertEqual(hw.clean("\x1b[31m你好\x1b[0m\x1b]52;c;BAD\x07\r\n"), "你好\n")
        self.assertEqual(hw.content_text('[{"type":"text","text":"正文"},{"type":"image"}]'), "正文\n[image]")

    def test_rendering_tools_and_hidden_content(self):
        self.add("tool", '{"output":"输出","exit_code":2}', tool="terminal")
        events = hw.Watcher(self.store, self.args("--tools")).poll()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for event in events:
                hw.render(event, self.args("--tools"))
        self.assertIn("输出", out.getvalue())
        self.assertIn("exit_code=2", out.getvalue())
        self.assertNotIn("HIDDEN_", out.getvalue())

    def test_cli_observes_external_concurrent_commits(self):
        self.add("user", "start-marker")
        def write_later():
            time.sleep(0.4)
            writer = sqlite3.connect(self.db, isolation_level=None)
            writer.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
                           ("one", "assistant", "LIVE_中文_MARKER", time.time()))
            writer.close()
        thread = threading.Thread(target=write_later)
        thread.start()
        result = subprocess.run([sys.executable, str(Path(hw.__file__)), "--db", str(self.db),
                                 "--jsonl", "--interval", "0.1", "--duration", "1.5"],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        thread.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(self.texts(events), ["start-marker", "LIVE_中文_MARKER"])
        self.assertNotIn("HIDDEN_", result.stdout)

    def test_global_merges_parallel_sessions_without_losing_messages(self):
        self.writer.execute("INSERT INTO sessions VALUES('two','第二个聊天','qqbot',2)")
        self.add("user", "first", "one")
        self.add("assistant", "second", "two")
        watcher = hw.GlobalWatcher(self.store, self.args("--global"))
        events = watcher.poll()
        self.assertEqual(events[0]["event"], "mode")
        self.assertEqual(self.texts(events), ["first", "second"])
        self.assertEqual([e["session_source"] for e in events if e["event"] == "message"], ["cli", "qqbot"])
        self.add("assistant", "one continued", "one")
        self.add("user", "two continued", "two")
        self.assertEqual(self.texts(watcher.poll()), ["one continued", "two continued"])
        self.assertEqual(watcher.poll(), [])
        self.writer.execute("INSERT INTO sessions VALUES('three','新会话','cli',3)")
        self.add("user", "new session", "three")
        self.assertEqual(self.texts(watcher.poll()), ["new session"])

    def test_global_filters_tail_and_in_place_updates(self):
        self.writer.execute("INSERT INTO sessions VALUES('two','other','qqbot',2)")
        self.add("user", "skip old")
        mid = self.add("assistant", "original")
        self.add("user", "other source", "two")
        watcher = hw.GlobalWatcher(self.store, self.args("--global", "--source", "cli", "--tail", "1"))
        self.assertEqual(self.texts(watcher.poll()), ["original"])
        self.writer.execute("UPDATE messages SET content='edited' WHERE id=?", (mid,))
        edited = watcher.poll()
        self.assertEqual(self.texts(edited), ["edited"])
        self.assertTrue(edited[0]["updated"])
        empty = hw.GlobalWatcher(self.store, self.args("--global", "--tail", "0"))
        self.assertEqual(self.texts(empty.poll()), [])
        self.add("assistant", "new from other", "two")
        self.assertEqual(self.texts(empty.poll()), ["new from other"])

    def test_global_cli_live_two_sources_and_machine_readable_output(self):
        def write_later():
            time.sleep(0.4)
            writer = sqlite3.connect(self.db, isolation_level=None)
            writer.execute("INSERT INTO sessions VALUES('two','另一个聊天','qqbot',2)")
            for sid, content in (("one", "CLI_实时"), ("two", "QQ_实时")):
                writer.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
                               (sid, "assistant", content, time.time()))
            writer.close()
        thread = threading.Thread(target=write_later)
        thread.start()
        result = subprocess.run([sys.executable, str(Path(hw.__file__)), "global", "--db", str(self.db),
                                 "--jsonl", "--color", "always", "--interval", "0.1", "--duration", "1.5"],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        thread.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(self.texts(events), ["CLI_实时", "QQ_实时"])
        self.assertNotIn("\x1b", result.stdout)
        self.assertNotIn("HIDDEN_", result.stdout)

    def test_global_rejects_a_pinned_session(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            hw.main(["global", "--session", "one"])
        self.assertEqual(exc.exception.code, 2)

    def test_narrow_pretty_output_and_thread_labels(self):
        self.add("user", "中文换行测试" * 15)
        args = self.args("--global", "--plain")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(hw.shutil, "get_terminal_size", return_value=os.terminal_size((44, 24))):
            renderer = hw.Renderer(args)
            for event in hw.GlobalWatcher(self.store, args).poll():
                renderer.render(event)
        text = output.getvalue()
        self.assertIn("用户", text)
        self.assertIn("测试会话", text)
        self.assertIn("#one", text)
        self.assertNotIn("\x1b", text)
        self.assertTrue(all(hw.cell_width(line) <= 43 for line in text.splitlines()))

    def test_message_width_tracks_terminal_resize(self):
        self.add("user", "窗口宽度测试")
        args = self.args("--plain")
        event = next(e for e in hw.Watcher(self.store, args).poll() if e["event"] == "message")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(hw.shutil, "get_terminal_size") as size:
            size.return_value = os.terminal_size((80, 24))
            renderer = hw.Renderer(args)
            renderer.render(event)
            size.return_value = os.terminal_size((180, 24))
            renderer.render(event)
            size.return_value = os.terminal_size((60, 24))
            renderer.render(event)
        borders = [line for line in output.getvalue().splitlines() if line.startswith("╭")]
        self.assertEqual([hw.cell_width(line) for line in borders], [79, 179, 59])

    def test_short_animation_restores_cursor_and_can_be_disabled(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        out = Terminal()
        args = self.args("--color", "always")
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(hw.time, "sleep") as delay:
            renderer = hw.Renderer(args)
            renderer.banner(self.db)
        self.assertIn("\x1b[?25l", out.getvalue())
        self.assertIn("\x1b[?25h", out.getvalue())
        self.assertLessEqual(sum(call.args[0] for call in delay.call_args_list), 0.6)
        out = Terminal()
        args.no_animation = True
        with contextlib.redirect_stdout(out), mock.patch.object(hw.time, "sleep") as delay:
            hw.Renderer(args).banner(self.db)
        self.assertNotIn("\x1b[?25l", out.getvalue())
        delay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
