# Synco

Synco is a Windows folder sync manager with saved sync jobs, scheduling, one-way sync, two-way sync, preview runs, and admin tools for managing job profiles.

## Run

Double-click `Run Synco.bat`.

If the app does not appear, double-click `Debug Synco.bat`. It keeps a command window open and shows startup details.

You can also run:

```powershell
py synco.py
```

## What it does

- Saves multiple sync jobs with names, folders, modes, and schedule settings.
- Shows a job dashboard with quick status cards for mode, schedule, and last run.
- Runs one-way sync from source to destination.
- Runs two-way sync where the newest file wins on either side.
- Offers preview mode so you can see what would change first.
- Offers mirror mode for one-way jobs to delete destination files when they no longer exist in the source.
- Runs scheduled sync jobs at a chosen minute interval while Synco is open.
- Includes admin tools to create, edit, duplicate, delete, import, and export sync jobs.
- Keeps file timestamps when files are copied.
- Blocks unsafe folder choices, like syncing a folder into itself.

Mirror mode can delete destination files. Use preview mode first when syncing important folders.

Two-way sync uses file size and modified time to decide what changed. If both sides have different content with the same timestamp, Synco reports a conflict instead of guessing.
