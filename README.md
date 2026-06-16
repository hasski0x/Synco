# Synco

Synco is a small Windows folder sync app.

## Run

Double-click `Run Synco.bat`, or run:

```powershell
py synco.py
```

## What it does

- Copies new and changed files from a source folder to a destination folder.
- Keeps file timestamps when files are copied.
- Offers preview mode so you can see what would change first.
- Offers mirror mode to delete files from the destination when they no longer exist in the source.
- Blocks unsafe folder choices, like syncing a folder into itself.

Mirror mode can delete destination files. Use preview mode first when syncing important folders.
