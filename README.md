# syncplan

Back up folders to a backup drive without surprises. syncplan first writes a **plan** listing every copy, overwrite and deletion it intends to make. It changes nothing until you have reviewed that plan and told it to go ahead.

> **Status: early development.** `syncplan init`, `add`, `remove` and `plan` work so far; `apply`, which carries a plan out, comes next. Don't rely on syncplan for your only copy of anything yet.

## How it will work

1. `syncplan init <backup> <sources>` marks a folder or drive as a syncplan backup of those source folders. Later runs refuse any destination without that marker, or whose marker doesn't list the source, so a changed drive letter can't point a sync at the wrong disk.
2. `syncplan plan <backup> <source>` compares a source folder with its copy on the backup and writes a plan file. Nothing else is modified.
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
- **Not a backup source.** syncplan's own folder, `C:\ProgramData\syncplan`, can't be one, so a whole `C:` drive is refused; back up its folders instead.

## Changing a backup's sources

```
usage: syncplan add [-h] BACKUP SOURCE [SOURCE ...]
usage: syncplan remove [-h] BACKUP NAME [NAME ...]
```

`add` backs up more folders to an existing backup. It takes sources exactly as `init` does, `NAME=SOURCE` included, and runs the same checks. It also refuses a folder that is already a source, inside one or holding one, and a subfolder name that is already taken:

```
> syncplan add E:\Backups D:\Music
Measuring sources...
Added to syncplan backup E:\Backups:
  Music <- D:\Music      45 GB
Space needed: about 45 GB. E:\ has 120 GB free.
Sources of E:\Backups now:
  Photos    <- C:\Photos
  Documents <- D:\Work\Documents
  Music     <- D:\Music
```

`remove` stops backing up sources, named by their subfolder name as listed. It works even when the source folder or its drive is gone:

```
> syncplan remove E:\Backups Photos
Removed from syncplan backup E:\Backups:
  Photos <- C:\Photos
The next plan will move E:\Backups\Photos to the quarantine; nothing moves until you apply that plan.
Sources of E:\Backups now:
  Documents <- D:\Work\Documents
  Music     <- D:\Music
Waiting for the quarantine:
  Photos (old copy of C:\Photos)
```

- **Nothing is deleted.** `remove` only changes the list. The old copy stays on the backup, recorded as waiting for the quarantine, and moves there only when you apply a plan.
- **Undo.** Add the same folder back under the same name, and its old copy is kept as its copy again.
- **A waiting name is taken.** While an old copy waits, another folder can't use its name.
- **All or nothing.** If anything in the command is wrong, such as a source already listed or a name that isn't, nothing changes.
- **Only the marker changes.** Both commands rewrite `E:\Backups\.syncplan\marker.json` and nothing else. The new list is written to a temporary file, flushed to disk and swapped in with one step, so the marker is never half-written. On drives formatted as exFAT or FAT32, an unplug at that exact moment is the one exception.

## Planning a backup

```
usage: syncplan plan [-h] [--quick] [--overwrite FILE [FILE ...]] BACKUP SOURCE
```

`plan` compares one source folder with its copy on the backup, and writes a plan of what would bring the copy up to date. It changes nothing else: no file in the source, and nothing on the backup but the plan file.

```
> syncplan plan E:\Backups C:\Photos
Listing C:\Photos and E:\Backups\Photos...
Reading 46,990 files to compare (290 GB)...
  12 GB of 290 GB read (1,900 of 46,990 files)
  ...
Plan for Photos (C:\Photos -> E:\Backups\Photos):
  copy (new)       1,204 files    12 GB
  overwrite           37 files   840 MB
  set date             5 files
  make folder          3 folders
  unchanged       46,950 files
To copy: 12.8 GB. E:\ has 120 GB free.
Plan written to E:\Backups\.syncplan\plans\Photos.json
Nothing has been changed.
```

- **What it plans.**
  - A new file is copied.
  - A file whose content differs and whose source date is later is overwritten; apply will keep the old copy in the quarantine.
  - A file whose only difference is its date gets the source's date.
  - A folder missing on the backup, even an empty one, is made.
- **Reading, or `--quick`.** By default plan reads both copies of every file that has the same size on both drives, and compares them byte for byte. That finds a file damaged on disk, and a change of date alone. It takes time: about an hour for 300 GB over USB 3. `--quick` compares only sizes and dates, and reads nothing.
- **Your decision.** In these cases plan touches neither copy and lists both copies' dates and sizes:
  - the content differs but the source's date isn't later: the same date with another size, the same date and size with different bytes (possible damage), or a backup copy newer than the source;
  - the backup's copy can't be read.

  If the source's file is right, plan again with `--overwrite C:\Photos\file.jpg`. If the backup's file is right, copy it back to the source yourself.
- **Skipped, and listed.** These are skipped and listed:
  - folders or files that can't be read;
  - links;
  - paths of 260 characters or more;
  - names Windows can't normally use;
  - a file on one side where the other has a folder;
  - a syncplan backup inside the source;
  - files that are online-only in OneDrive;
  - files that change while plan reads them.
- **Refused.** plan refuses:
  - a source the backup doesn't list;
  - a source whose folder is missing (deleted, or its drive unplugged);
  - a backup drive that isn't formatted as NTFS.
- **The plan file.** `<backup>\.syncplan\plans\<NAME>.json`, one per source, replaced by each plan and written even when there is nothing to do. It is JSON, readable in Notepad.
- **Files no longer in the source.** Files on the backup that are no longer in the source are counted, and left as they are for now.

## Logs

Every run writes what it did, step by step, to a log file on your PC:

```
C:\ProgramData\syncplan\logs\syncplan.log
```

- **What it holds.** Each run gets a short run ID. The log records the syncplan, Python and Windows versions, the exact command, each check and its outcome, the sizes and timings measured, what was written, and how the run ended. For an error, it also holds the full technical details.
- **On screen.** You see the normal messages; warnings and errors go to the error output. An error message ends with `Details:` and the log's location.
- **Size.** When the log reaches 1 MB (1,048,576 bytes), the next run starts a new one. syncplan keeps the 5 previous logs (`syncplan.log.1` to `.5`), so they never take more than about 6 MB.
- **Location.** `C:\ProgramData` is a hidden Windows folder outside your user folder, so backing up your files never includes the log.
- **Real files only.** If `C:\ProgramData\syncplan`, a folder above it, or the log itself is a link to somewhere else, syncplan refuses to run. Otherwise its log could end up inside a folder you back up.
- **One run at a time.** syncplan runs one command at a time on a PC. A second window is refused until the first one finishes.
- **On a shared PC,** every Windows account on it can read the log, which holds folder names and commands.
- **If you see "couldn't write the log"** after once running syncplan as administrator, that run made the log folder belong to administrators. Delete `C:\ProgramData\syncplan` from an administrator window, and the next normal run recreates it.

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
