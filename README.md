# syncplan

Back up folders to a backup drive without surprises. syncplan first writes a **plan** listing every copy, overwrite and deletion it intends to make. It changes nothing until you have reviewed that plan and told it to go ahead.

> **Status: early development.** The commands below are the plan, not yet working features. Don't rely on syncplan for your only copy of anything yet.

## How it will work

1. `syncplan init <backup>` marks a drive as a syncplan backup. Later runs refuse any destination without that marker, so a changed drive letter can't point a sync at the wrong disk.
2. `syncplan plan` compares your configured source folders with the backup and writes a plan file. Nothing is modified.
3. You read the plan.
4. `syncplan apply <plan>` carries out exactly that plan and nothing else.

The backup becomes a mirror of your sources. Source folders are only ever read, never written. When a plan deletes or overwrites a file on the backup, the old copy is moved to a dated quarantine folder on the backup drive instead of being destroyed, so a mistaken sync can be undone.

## Install

You need [uv](https://docs.astral.sh/uv/). uv downloads Python 3.14 itself if it isn't installed. syncplan is built and tested on Windows.

syncplan is a console script: `pyproject.toml` declares `syncplan = "syncplan.cli:main"`, and `uv tool install` turns that into a `syncplan` command.

1. Install it, either straight from GitHub:

   ```
   uv tool install git+https://github.com/aleromar/syncplan
   ```

   or from a local clone in editable mode, where code changes take effect without reinstalling:

   ```
   git clone https://github.com/aleromar/syncplan
   cd syncplan
   uv tool install -e .
   ```

2. The first time you install a uv tool, add uv's tool folder (`%USERPROFILE%\.local\bin`) to your PATH, then open a new terminal:

   ```
   uv tool update-shell
   ```

3. Check that it works from any directory:

   ```
   syncplan --version
   ```

Remove it with `uv tool uninstall syncplan`.

To run it from a clone without installing anything, use `uv run syncplan --version`.

## Development

```
uv sync                   # create .venv with the dev tools
uv run pytest             # run the tests
uv run ruff check --fix   # lint
uv run ruff format        # format
```

CI runs the same checks on Windows for every push to `main` and every pull request.

Each feature is built spec-first and test-first, and lands as one small pull request.

## License

[MIT](LICENSE)
