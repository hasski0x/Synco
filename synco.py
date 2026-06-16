import os
import queue
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from tkinter import BooleanVar, END, StringVar, Tk, filedialog, messagebox, ttk


APP_NAME = "Synco"
BUFFER_SIZE = 1024 * 1024


@dataclass
class SyncStats:
    copied: int = 0
    skipped: int = 0
    deleted: int = 0
    errors: int = 0


class SyncCancelled(Exception):
    pass


class FolderSyncer:
    def __init__(self, source, destination, mirror, dry_run, log, should_cancel):
        self.source = Path(source).resolve()
        self.destination = Path(destination).resolve()
        self.mirror = mirror
        self.dry_run = dry_run
        self.log = log
        self.should_cancel = should_cancel
        self.stats = SyncStats()

    def run(self):
        self._validate()
        self.destination.mkdir(parents=True, exist_ok=True)
        self.log(f"Syncing {self.source} -> {self.destination}")
        if self.dry_run:
            self.log("Preview mode is on. No files will be changed.")

        self._copy_newer_files()
        if self.mirror:
            self._delete_extra_files()

        return self.stats

    def _validate(self):
        if not self.source.exists() or not self.source.is_dir():
            raise ValueError("Choose a valid source folder.")

        source_text = str(self.source).lower()
        destination_text = str(self.destination).lower()
        if source_text == destination_text:
            raise ValueError("Source and destination must be different folders.")

        if destination_text.startswith(source_text + os.sep):
            raise ValueError("Destination cannot be inside the source folder.")

        if source_text.startswith(destination_text + os.sep):
            raise ValueError("Source cannot be inside the destination folder.")

    def _copy_newer_files(self):
        for source_file in self.source.rglob("*"):
            self._check_cancelled()
            if not source_file.is_file():
                continue

            relative_path = source_file.relative_to(self.source)
            destination_file = self.destination / relative_path

            try:
                if self._needs_copy(source_file, destination_file):
                    self._copy_file(source_file, destination_file)
                    self.stats.copied += 1
                else:
                    self.stats.skipped += 1
            except Exception as exc:
                self.stats.errors += 1
                self.log(f"Error: {relative_path} ({exc})")

    def _delete_extra_files(self):
        if not self.destination.exists():
            return

        for destination_file in sorted(self.destination.rglob("*"), reverse=True):
            self._check_cancelled()
            relative_path = destination_file.relative_to(self.destination)
            source_match = self.source / relative_path

            if source_match.exists():
                continue

            try:
                if destination_file.is_file() or destination_file.is_symlink():
                    self._delete_file(destination_file)
                    self.stats.deleted += 1
                elif destination_file.is_dir() and not any(destination_file.iterdir()):
                    self._delete_directory(destination_file)
            except Exception as exc:
                self.stats.errors += 1
                self.log(f"Error deleting {relative_path} ({exc})")

    def _needs_copy(self, source_file, destination_file):
        if not destination_file.exists():
            return True

        source_stat = source_file.stat()
        destination_stat = destination_file.stat()
        size_changed = source_stat.st_size != destination_stat.st_size
        source_newer = source_stat.st_mtime > destination_stat.st_mtime + 1
        return size_changed or source_newer

    def _copy_file(self, source_file, destination_file):
        relative_path = source_file.relative_to(self.source)
        self.log(f"Copy: {relative_path}")

        if self.dry_run:
            return

        destination_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = destination_file.with_name(destination_file.name + ".synco_tmp")

        with source_file.open("rb") as read_file, temp_file.open("wb") as write_file:
            while True:
                self._check_cancelled()
                chunk = read_file.read(BUFFER_SIZE)
                if not chunk:
                    break
                write_file.write(chunk)

        shutil.copystat(source_file, temp_file)
        temp_file.replace(destination_file)

    def _delete_file(self, destination_file):
        relative_path = destination_file.relative_to(self.destination)
        self.log(f"Delete: {relative_path}")

        if not self.dry_run:
            destination_file.unlink()

    def _delete_directory(self, destination_directory):
        relative_path = destination_directory.relative_to(self.destination)
        self.log(f"Remove empty folder: {relative_path}")

        if not self.dry_run:
            destination_directory.rmdir()

    def _check_cancelled(self):
        if self.should_cancel():
            raise SyncCancelled()


class SyncoApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("820x560")
        self.root.minsize(720, 500)

        self.source = StringVar()
        self.destination = StringVar()
        self.mirror = BooleanVar(value=False)
        self.dry_run = BooleanVar(value=False)
        self.status = StringVar(value="Choose two folders to get started.")

        self.messages = queue.Queue()
        self.worker = None
        self.cancel_requested = False

        self._build_ui()
        self._poll_messages()

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        title = ttk.Label(self.root, text=APP_NAME, font=("Segoe UI", 20, "bold"))
        title.grid(row=0, column=0, sticky="w", padx=18, pady=(16, 4))

        panel = ttk.Frame(self.root, padding=18)
        panel.grid(row=1, column=0, sticky="ew")
        panel.columnconfigure(1, weight=1)

        self._folder_row(panel, 0, "Source", self.source, self._choose_source)
        self._folder_row(panel, 1, "Destination", self.destination, self._choose_destination)

        options = ttk.Frame(panel)
        options.grid(row=2, column=1, sticky="w", pady=(10, 0))
        ttk.Checkbutton(options, text="Mirror destination", variable=self.mirror).grid(row=0, column=0, padx=(0, 22))
        ttk.Checkbutton(options, text="Preview only", variable=self.dry_run).grid(row=0, column=1)

        actions = ttk.Frame(panel)
        actions.grid(row=3, column=1, sticky="w", pady=(16, 0))
        self.sync_button = ttk.Button(actions, text="Start sync", command=self._start_sync)
        self.sync_button.grid(row=0, column=0, padx=(0, 10))
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self._cancel_sync, state="disabled")
        self.cancel_button.grid(row=0, column=1)

        log_frame = ttk.Frame(self.root, padding=(18, 0, 18, 12))
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_box = ttk.Treeview(log_frame, columns=("message",), show="headings", height=16)
        self.log_box.heading("message", text="Activity")
        self.log_box.column("message", anchor="w", stretch=True)
        self.log_box.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=scrollbar.set)

        status_bar = ttk.Label(self.root, textvariable=self.status, relief="sunken", anchor="w", padding=(8, 4))
        status_bar.grid(row=3, column=0, sticky="ew")

    def _folder_row(self, parent, row, label, variable, command):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 12))
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(parent, text="Browse", command=command).grid(row=row, column=2, sticky="e", padx=(10, 0), pady=5)

    def _choose_source(self):
        folder = filedialog.askdirectory(title="Choose source folder")
        if folder:
            self.source.set(folder)

    def _choose_destination(self):
        folder = filedialog.askdirectory(title="Choose destination folder")
        if folder:
            self.destination.set(folder)

    def _start_sync(self):
        if self.worker and self.worker.is_alive():
            return

        self.cancel_requested = False
        self.sync_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status.set("Sync running...")
        self._clear_log()

        syncer = FolderSyncer(
            self.source.get(),
            self.destination.get(),
            self.mirror.get(),
            self.dry_run.get(),
            self._queue_log,
            lambda: self.cancel_requested,
        )

        self.worker = threading.Thread(target=self._run_sync, args=(syncer,), daemon=True)
        self.worker.start()

    def _run_sync(self, syncer):
        started_at = time.time()
        try:
            stats = syncer.run()
            elapsed = time.time() - started_at
            summary = (
                f"Done in {elapsed:.1f}s. "
                f"Copied {stats.copied}, skipped {stats.skipped}, "
                f"deleted {stats.deleted}, errors {stats.errors}."
            )
            self.messages.put(("done", summary))
        except SyncCancelled:
            self.messages.put(("done", "Sync cancelled."))
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def _cancel_sync(self):
        self.cancel_requested = True
        self.status.set("Cancelling...")

    def _queue_log(self, message):
        self.messages.put(("log", message))

    def _poll_messages(self):
        while True:
            try:
                kind, text = self.messages.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._append_log(text)
            elif kind == "done":
                self._append_log(text)
                self.status.set(text)
                self._set_idle()
            elif kind == "error":
                self.status.set("Sync failed.")
                self._append_log(f"Error: {text}")
                messagebox.showerror(APP_NAME, text)
                self._set_idle()

        self.root.after(100, self._poll_messages)

    def _append_log(self, message):
        self.log_box.insert("", END, values=(message,))
        children = self.log_box.get_children()
        if children:
            self.log_box.see(children[-1])

    def _clear_log(self):
        for item in self.log_box.get_children():
            self.log_box.delete(item)

    def _set_idle(self):
        self.sync_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")


def main():
    root = Tk()
    SyncoApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
