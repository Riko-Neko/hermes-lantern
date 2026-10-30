# Hermes Lantern · 提灯

A lightweight, read-only terminal viewer for [Hermes Agent](https://github.com/NousResearch/hermes-agent) conversations. Follow the latest session or watch messages across sessions in one database. It uses the Python standard library and does not start a model or modify the database. The interface is currently in Chinese.

一个只读的 Hermes 对话终端查看器：可以跟随最新会话，也可以实时合流同一数据库里的多个会话。无需安装第三方 Python 依赖。

## Getting started

Requires Python 3.10 or newer and a compatible Hermes `state.db` database. Download or clone this repository, then run:

```sh
python hermes_lantern.py                 # follow the latest active session
python hermes_lantern.py global          # merge all sessions
python hermes_lantern.py global --tools  # include tool calls and results
python hermes_lantern.py --list          # find session IDs
python hermes_lantern.py SESSION_ID --all-history
python hermes_lantern.py --db /path/to/state.db
```

On Windows, `hermes-lantern.cmd` uses the Python launcher (`py -3`) and can be run from the repository folder. For a command available elsewhere, add that folder to your PATH. Stop watching with Ctrl+C. Use `--help` for all options.

## How it works

- Opens SQLite in read-only mode with `query_only`, and releases read transactions between polls. By default it checks `HERMES_HOME/state.db`, Windows `%LOCALAPPDATA%/hermes/state.db`, `~/.hermes/state.db`, then `~/.local/share/hermes/state.db`; `--db` selects a different database.
- Shows user and assistant messages by default. `--tools` also displays tool calls and results, which may contain sensitive information. `--jsonl` emits machine-readable events; `--plain` disables ANSI styling.
- Polls once per second by default. Only committed database messages are visible; this is not token-by-token streaming. Changes to recent messages are shown as updates. Global mode follows all sessions in the selected database.
- A compact terminal animation and colored message cards help distinguish roles and conversations. Animation and colors are disabled automatically for redirected output.

Chat transcripts and tool output may contain private information. Take care when sharing terminal output, especially in global mode.

## Development

```sh
python -m unittest discover -v
```

Tests use temporary databases, including concurrent WAL writes, multi-session following, filtering and read-only behavior.

## Credits

Created at the request of Riko-Neko. Codex implemented the viewer. The Hermes agent helped locate the Hermes CLI banner and suggested an animation mechanism; it did not implement the viewer. The startup design was inspired by the Hermes CLI banner. This is an independent community project and is not an official Hermes Agent product.
