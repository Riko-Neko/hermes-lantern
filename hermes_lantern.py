"""Read-only, live Hermes chat viewer. Python standard library only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime

VERSION = "2.0.0"
COMMAND = "hermes-lantern"
FIELDS = "id, session_id, role, content, tool_name, tool_calls, timestamp"


def clean(value: object) -> str:
    """Keep transcripts from emitting terminal control sequences."""
    text = str(value or "")
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) < 160))


def content_text(value: str | None) -> str:
    if not value:
        return ""
    if value.lstrip().startswith("["):
        try:
            blocks = json.loads(value)
            if isinstance(blocks, list) and all(isinstance(b, dict) for b in blocks):
                return "\n".join(str(b.get("text") or "[" + str(b.get("type", "attachment")) + "]") for b in blocks)
        except (ValueError, TypeError):
            pass
    return value


def default_db() -> Path:
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]) / "state.db"
    candidates = []
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "hermes" / "state.db")
    candidates.extend([Path.home() / ".hermes/state.db", Path.home() / ".local/share/hermes/state.db"])
    return next((p for p in candidates if p.is_file()), candidates[0])


class Store:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        if not self.path.is_file():
            raise ValueError(f"找不到 Hermes 数据库：{self.path}；可用 --db 指定。")
        self.conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.5, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")
        for table, required in (("sessions", {"id", "title", "source", "started_at"}),
                                ("messages", set(FIELDS.replace(" ", "").split(",")))):
            columns = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not required <= columns:
                self.close()
                raise ValueError(f"不兼容的 Hermes 数据库：{table} 缺少 {sorted(required - columns)}")

    def close(self):
        self.conn.close()

    def sessions(self, source=None, limit=30):
        where, params = ("WHERE s.source=?", [source]) if source else ("", [])
        sql = f"""SELECT s.id,s.title,s.source,s.started_at,
                   COALESCE((SELECT MAX(m.id) FROM messages m WHERE m.session_id=s.id),0) AS last_id
                   FROM sessions s {where} ORDER BY last_id DESC,s.started_at DESC LIMIT ?"""
        return [dict(r) for r in self.conn.execute(sql, [*params, limit]).fetchall()]

    def session(self, session_id):
        row = self.conn.execute("SELECT id,title,source,started_at FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"找不到会话：{session_id}；用 --list 查看会话 ID。")
        return dict(row)

    def messages(self, session_id, *, tools=False, tail=None, since=0, inclusive=False):
        visible = "role IN ('user','assistant','tool')" if tools else "role IN ('user','assistant') AND content IS NOT NULL AND content<>''"
        comparison = ">=" if inclusive else ">"
        params = [session_id, since]
        sql = f"SELECT {FIELDS} FROM messages WHERE session_id=? AND id{comparison}? AND {visible} ORDER BY id"
        if tail is not None:
            sql += " DESC LIMIT ?"
            params.append(tail)
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        return list(reversed(rows)) if tail is not None else rows

    def global_messages(self, *, tools=False, source=None, tail=None, since=0, inclusive=False):
        visible = "m.role IN ('user','assistant','tool')" if tools else "m.role IN ('user','assistant') AND m.content IS NOT NULL AND m.content<>''"
        comparison = ">=" if inclusive else ">"
        fields = ",".join("m." + f.strip() for f in FIELDS.split(","))
        sql = f"""SELECT {fields},s.title AS session_title,s.source AS session_source
                  FROM messages m JOIN sessions s ON s.id=m.session_id
                  WHERE m.id{comparison}? AND {visible}"""
        params = [since]
        if source:
            sql += " AND s.source=?"
            params.append(source)
        sql += " ORDER BY m.id"
        if tail is not None:
            sql += " DESC LIMIT ?"
            params.append(tail)
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        return list(reversed(rows)) if tail is not None else rows

    def max_id(self, session_id=None):
        if session_id is None:
            return self.conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
        return self.conn.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0]


class Watcher:
    def __init__(self, store, args):
        self.store, self.args = store, args
        self.current = None
        self.states = {}
        self.waiting = False

    def poll(self):
        if self.args.session:
            session = self.store.session(self.args.session)
        else:
            sessions = self.store.sessions(self.args.source, 1)
            session = sessions[0] if sessions else None
        if not session:
            if self.waiting:
                return []
            self.waiting = True
            return [{"event": "waiting", "text": "暂无会话，等待新聊天……"}]
        events = []
        sid = session["id"]
        if sid != self.current:
            events.append({"event": "session", **session})
            self.current = sid
        if sid not in self.states:
            initial_cursor = self.store.max_id(sid)
            rows = self.store.messages(sid, tools=self.args.tools, tail=None if self.args.all_history else self.args.tail)
            self.states[sid] = {"cursor": initial_cursor if not rows else 0, "seen": {}}
        else:
            state = self.states[sid]
            floor = min(state["seen"], default=state["cursor"])
            rows = self.store.messages(sid, tools=self.args.tools, since=floor, inclusive=bool(state["seen"]))
        state = self.states[sid]
        for row in rows:
            fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            old = state["seen"].get(row["id"])
            if old != fingerprint:
                events.append({"event": "message", "updated": old is not None,
                               "session_title": session.get("title"), "session_source": session.get("source"), **row})
            state["seen"][row["id"]] = fingerprint
            state["cursor"] = max(state["cursor"], row["id"])
        # Recheck recent rows for in-place edits, without keeping whole transcripts in memory.
        state["seen"] = {i: state["seen"][i] for i in sorted(state["seen"])[-32:]}
        return events


class GlobalWatcher:
    """One database-wide watermark: parallel sessions cannot hide each other."""

    def __init__(self, store, args):
        self.store, self.args = store, args
        self.started = False
        self.cursor = 0
        self.seen = {}

    def poll(self):
        events = []
        if not self.started:
            self.cursor = self.store.max_id()
            rows = self.store.global_messages(tools=self.args.tools, source=self.args.source,
                                             tail=None if self.args.all_history else self.args.tail)
            events.append({"event": "mode", "mode": "global", "text": "GLOBAL · 所有会话，按消息写入顺序合流"})
            self.started = True
        else:
            rows = self.store.global_messages(tools=self.args.tools, source=self.args.source,
                                             since=min(self.seen, default=self.cursor), inclusive=bool(self.seen))
        for row in rows:
            fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            old = self.seen.get(row["id"])
            if old != fingerprint:
                events.append({"event": "message", "updated": old is not None, **row})
            self.seen[row["id"]] = fingerprint
            self.cursor = max(self.cursor, row["id"])
        self.seen = {i: self.seen[i] for i in sorted(self.seen)[-256:]}
        return events


def clip(text, limit):
    return text if limit == 0 or len(text) <= limit else text[:limit] + f"\n…（已截断；--tool-limit 0 显示完整工具输出）"


def message_text(event, args):
    text = content_text(event.get("content"))
    if event["role"] == "tool":
        try:
            result = json.loads(text)
            if isinstance(result, dict) and "output" in result:
                text = str(result["output"] or "")
                if result.get("exit_code") not in (None, 0):
                    text += f"\nexit_code={result['exit_code']}"
                if result.get("error"):
                    text += f"\nerror={result['error']}"
        except (ValueError, TypeError):
            pass
        text = clip(text, args.tool_limit)
    if args.tools and event.get("tool_calls"):
        try:
            calls = json.loads(event["tool_calls"]) if isinstance(event["tool_calls"], str) else event["tool_calls"]
            for call in calls:
                function = call.get("function", call)
                arguments = function.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                text += "\n调用 " + str(function.get("name", "tool")) + "\n" + clip(arguments, args.tool_limit)
        except (ValueError, TypeError, AttributeError):
            text += "\n[工具调用记录格式无法解析]"
    return clean(text).expandtabs(4)


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def fit(text, width):
    text = clean(text).replace("\n", " ").expandtabs(4)
    if cell_width(text) <= width:
        return text
    result = ""
    for char in text:
        if cell_width(result + char) > width - 1:
            break
        result += char
    return result + "…"


def wrap_lines(text, width):
    for line in text.split("\n"):
        buffer, size = "", 0
        for char in line:
            char_size = cell_width(char)
            if size + char_size > width and buffer:
                yield buffer
                buffer, size = "", 0
            buffer += char
            size += char_size
        yield buffer


def enable_windows_ansi():
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.SetConsoleMode.restype = wintypes.BOOL
        handle = kernel.GetStdHandle(-11)
        mode = wintypes.DWORD()
        return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and kernel.SetConsoleMode(handle, mode.value | 4))
    except (AttributeError, OSError):
        return False


class Renderer:
    ROLE_COLORS = {"user": 45, "assistant": 220, "tool": 141}
    THREAD_COLORS = (81, 114, 215, 177, 153, 222)

    def __init__(self, args):
        self.args = args
        self.width = self.terminal_width()
        requested = not args.plain and args.color != "never" and "NO_COLOR" not in os.environ
        self.color = requested and (args.color == "always" or (sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and enable_windows_ansi()))

    def paint(self, text, color=245, bold=False):
        if not self.color:
            return text
        return f"\x1b[{1 if bold else 0};38;5;{color}m{text}\x1b[0m"

    @staticmethod
    def terminal_width():
        return max(24, shutil.get_terminal_size((88, 24)).columns - 1)

    def banner(self, path):
        if self.args.jsonl:
            return
        mode = "GLOBAL / 全部会话" if self.args.global_mode else "PINNED / 固定会话" if self.args.session else "FOLLOW / 最新活跃"
        lines = ["   \\___|___/    HERMES / LANTERN",
                 f"   <---*--->    {mode}",
                 f"       |        READ ONLY · {self.args.interval:g}s · Ctrl+C 退出"]
        if self.width < 50:
            lines = ["  * HERMES / LANTERN", f"  {mode}", f"  只读 · {self.args.interval:g}s · Ctrl+C 退出"]
        lines = [fit(line, self.width) for line in lines]
        animate = self.color and sys.stdout.isatty() and not self.args.no_animation and not self.args.once and not self.args.plain
        # Original compact wing/staff artwork; a sub-second gold sweep inspired by Hermes.
        frames = 7 if animate else 1
        if animate:
            print("\x1b[?25l", end="", flush=True)
        try:
            for frame in range(frames):
                if frame:
                    print(f"\x1b[{len(lines)}F", end="")
                for index, line in enumerate(lines):
                    if animate and frame < frames - 1:
                        focus = frame * max(1, self.width // 6)
                        rendered = "".join(self.paint(c, 229 if abs(j - focus) < 6 else 214 if abs(j - focus) < 14 else 136, index == 0)
                                           for j, c in enumerate(line))
                    else:
                        rendered = self.paint(line, (220, 214, 136)[index], index == 0)
                    print(("\x1b[2K" if animate else "") + rendered)
                sys.stdout.flush()
                if animate:
                    time.sleep(0.06)
        finally:
            if animate:
                print("\x1b[?25h", end="", flush=True)
        print(self.paint("  v" + VERSION + " · " + fit(str(path), self.width - 14), 245), flush=True)

    def render(self, event):
        if self.args.jsonl:
            print(json.dumps(event, ensure_ascii=False), flush=True)
            return
        self.width = self.terminal_width()
        if event["event"] in ("waiting", "mode"):
            print(flush=True)
            for line in wrap_lines(event["text"], self.width - 2):
                print(self.paint("  " + line, 136), flush=True)
            return
        if event["event"] == "session":
            print("\n" + self.paint("─" * self.width, 136))
            print(self.paint("  " + fit(event.get("title") or "无标题", self.width - 2), 220, True))
            print(self.paint("  " + fit(str(event.get("source")) + " · " + event["id"], self.width - 2), 245), flush=True)
            return
        text = message_text(event, self.args)
        if not text.strip():
            return
        role = {"user": "用户", "assistant": "Hermes", "tool": "工具"}.get(event["role"], event["role"])
        if event["role"] == "tool":
            role += " / " + str(event.get("tool_name") or "result")
        try:
            stamp = datetime.fromtimestamp(float(event["timestamp"])).strftime("%H:%M:%S")
        except (ValueError, TypeError, OSError, OverflowError):
            stamp = "--:--:--"
        color = self.ROLE_COLORS.get(event["role"], 245)
        sid = event["session_id"]
        border_color = self.THREAD_COLORS[int(hashlib.sha256(sid.encode()).hexdigest()[:8], 16) % len(self.THREAD_COLORS)] if self.args.global_mode else color
        updated = " · 更新" if event.get("updated") else ""
        label = fit(f" {role} · {stamp}{updated} ", self.width - 4)
        print("\n" + self.paint("╭─", border_color) + self.paint(label, color, True)
              + self.paint("─" * max(0, self.width - 3 - cell_width(label)) + "╮", border_color))
        if self.args.global_mode:
            meta = f"{event.get('session_source') or '?'} · {event.get('session_title') or '无标题'}"
            self.body_line(fit(meta, self.width - 4), border_color, dim=True)
            self.body_line(fit("#" + sid, self.width - 4), border_color, dim=True)
            print(self.paint("├" + "─" * (self.width - 2) + "┤", border_color))
        for line in wrap_lines(text, self.width - 4):
            self.body_line(line, border_color)
        print(self.paint("╰" + "─" * (self.width - 2) + "╯", border_color), flush=True)

    def body_line(self, text, color, dim=False):
        padding = " " * max(0, self.width - 4 - cell_width(text))
        print(self.paint("│", color) + " " + (self.paint(text, 245) if dim else text) + padding + " " + self.paint("│", color))


def render(event, args):
    Renderer(args).render(event)


def parser():
    p = argparse.ArgumentParser(prog=COMMAND, description="Hermes Lantern · 只读实时聊天。默认跟随最新活跃会话；global 合流全部会话。")
    p.add_argument("session_id", nargs="?", help="会话 ID，或 global；省略则跟随最新活跃会话")
    p.add_argument("--session", help="固定会话 ID，与位置参数二选一")
    p.add_argument("--global", dest="global_mode", action="store_true", help="跨全部会话，按消息写入顺序实时合流")
    p.add_argument("--db", type=Path, help="指定 state.db；默认自动查找 Hermes 数据目录")
    p.add_argument("--list", action="store_true", help="列出最近活跃的会话后退出")
    p.add_argument("--source", help="自动选择时只看指定来源，例如 cli 或 qqbot")
    p.add_argument("--tail", type=int, default=20, help="首次显示的最近消息条数，0 表示只看新消息（默认 20）")
    p.add_argument("--all-history", action="store_true", help="首次显示选定会话的完整历史")
    p.add_argument("--tools", action="store_true", help="同时显示工具调用与结果（默认仅聊天正文）")
    p.add_argument("--tool-limit", type=int, default=2000, help="工具内容最大字符数，0 不截断（默认 2000）")
    p.add_argument("--interval", type=float, default=1, help="刷新间隔秒数（默认 1）")
    p.add_argument("--once", action="store_true", help="显示当前快照后退出")
    p.add_argument("--duration", type=float, help="最多运行多少秒；省略则一直运行")
    p.add_argument("--jsonl", action="store_true", help="输出 JSONL 事件，方便管道处理")
    p.add_argument("--no-animation", action="store_true", help="跳过约半秒的启动光扫动画")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="颜色输出（默认 auto）")
    p.add_argument("--plain", action="store_true", help="纯文本输出，关闭 ANSI 颜色和动画")
    p.add_argument("--version", action="version", version=f"{COMMAND} {VERSION}")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.session_id == "global":
        args.global_mode = True
        args.session_id = None
    if args.session and args.session_id:
        p.error("会话 ID 只需指定一次")
    args.session = args.session or args.session_id
    if args.global_mode and args.session:
        p.error("global 模式不能同时固定单个会话")
    if args.interval < 0.1 or args.tail < 0 or args.tool_limit < 0 or (args.duration is not None and args.duration <= 0):
        p.error("interval 至少为 0.1；tail/tool-limit 不可为负；duration 必须大于 0")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    store = None
    try:
        store = Store(args.db or default_db())
        if args.list:
            for s in store.sessions(args.source):
                if args.jsonl:
                    print(json.dumps(s, ensure_ascii=False))
                else:
                    print(f"{clean(s['id'])}  {clean(s['source']):8}  {clean(s['title'] or '无标题')}")
            return 0
        renderer = Renderer(args)
        renderer.banner(store.path)
        watcher = GlobalWatcher(store, args) if args.global_mode else Watcher(store, args)
        deadline = time.monotonic() + args.duration if args.duration is not None else None
        last_error = None
        while True:
            try:
                for event in watcher.poll():
                    renderer.render(event)
                last_error = None
            except sqlite3.OperationalError as exc:
                if args.once:
                    raise
                if str(exc) != last_error:
                    print(f"暂时无法读取数据库，稍后重试：{exc}", file=sys.stderr, flush=True)
                    last_error = str(exc)
            if args.once or (deadline is not None and time.monotonic() >= deadline):
                break
            time.sleep(min(args.interval, max(0, deadline - time.monotonic())) if deadline else args.interval)
        return 0
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"{COMMAND}：{exc}", file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
