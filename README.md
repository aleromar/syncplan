# syncplan

Back up folders to a backup drive without surprises. syncplan first writes a **plan** listing every copy, overwrite and deletion it intends to make. It changes nothing until you have reviewed that plan and told it to go ahead.

> **Status: early development.** Only `syncplan init` works so far; the other commands are the plan. Don't rely on syncplan for your only copy of anything yet.

## How it will work

1. `syncplan init <backup> <sources>` marks a folder or drive as a syncplan backup of those source folders. Later runs refuse any destination without that marker, or whose marker doesn't list the source, so a changed drive letter can't point a sync at the wrong disk.
2. `syncplan plan` compares your source folders with the backup and writes a plan file. Nothing is modified.
3. You read the plan.
4. `syncplan apply <plan>` carries out exactly that plan and nothing else.

The backup becomes a mirror of your sources. Source folders are only ever read, never written. When a plan deletes or overwrites a file on the backup, the old copy is moved to a dated quarantine folder on the backup drive instead of being destroyed, so a mistaken sync can be undone.

## Setting up a backup

```
usage: syncplan init [-h] BACKUP [SOURCE ...]
```

`BACKUP` is an existing folder, or a whole drive such as `E:`. Each `SOURCE` is a folder to back up, on any drive. Each source gets its own subfolder on the backup, named after the source folder:

```
> syncplan init E:\Backups C:\Photos D:\Work\Documents
Measuring sources...
Initialized syncplan backup at E:\Backups
  Photos    <- C:\Photos             312 GB
  Documents <- D:\Work\Documents      88 GB
Space needed: about 400 GB. E:\ has 1.8 TB free.
```

- **Choosing a subfolder name.** Write `NAME=SOURCE`, for example `WorkDocs=D:\Documents`. You need it when two sources have the same folder name, or when a source is a whole drive.
- **What init writes.** One small file, `E:\Backups\.syncplan\marker.json`, listing the source folders. Nothing is copied, and anything already in the backup folder is left as it is. A folder already named like a source, such as an old manual copy, becomes that source's mirror.
- **What init refuses.** It writes nothing and exits with code 1 when:
  - the backup folder doesn't exist, or is already a backup or inside one;
  - a source doesn't exist, or is a syncplan backup;
  - a source and the backup overlap, or two sources overlap;
  - a subfolder name can't be used on Windows.
- **Space.** It estimates the space needed and warns, without stopping, if the drive looks too small or would be left with less than 10% free.
- **Redundancy.** You can back up the same sources to several drives: run `init` once per drive.

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
