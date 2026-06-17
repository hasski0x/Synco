import json
import os
import queue
import shutil
import traceback
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import BooleanVar, END, IntVar, StringVar, Tk, filedialog, messagebox, ttk


APP_NAME = "Synco"
APP_VERSION = "0.2.0"
BUFFER_SIZE = 1024 * 1024
TIME_TOLERANCE_SECONDS = 1
ONE_WAY = "One-way"
TWO_WAY = "Two-way"
SCHEDULE_MINUTES = "Minutes"
SCHEDULE_DAYS = "Days"


def app_data_dir():
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


JOBS_FILE = app_data_dir() / "jobs.json"
ERROR_LOG = app_data_dir() / "error.log"


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def log_error(exc):
    with ERROR_LOG.open("a", encoding="utf-8") as file:
        file.write(f"\n[{now_iso()}] {exc}\n")
        file.write(traceback.format_exc())
        file.write("\n")


def bring_window_forward(root):
    root.update_idletasks()
    width = root.winfo_width()
    height = root.winfo_height()
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    left = max(0, int((screen_width - width) / 2))
    top = max(0, int((screen_height - height) / 2))
    root.geometry(f"{width}x{height}+{left}+{top}")
    root.deiconify()
    root.lift()
    root.attributes("-topmost", True)
    root.after(900, lambda: root.attributes("-topmost", False))
    root.focus_force()


def schedule_label(job):
    amount = max(1, int(job.schedule_amount or 1))
    if job.schedule_unit == SCHEDULE_DAYS:
        unit = "day" if amount == 1 else "days"
        return f"Every {amount} {unit}"
    unit = "minute" if amount == 1 else "minutes"
    return f"Every {amount} {unit}"


@dataclass
class SyncStats:
    copied: int = 0
    skipped: int = 0
    deleted: int = 0
    conflicts: int = 0
    errors: int = 0


@dataclass
class SyncJob:
    name: str = "New Sync Job"
    source: str = ""
    destination: str = ""
    mode: str = ONE_WAY
    mirror: bool = False
    dry_run: bool = False
    enabled: bool = True
    schedule_enabled: bool = False
    interval_minutes: int = 60
    schedule_unit: str = SCHEDULE_MINUTES
    schedule_amount: int = 60
    last_run: str = ""
    next_run: str = ""
    last_status: str = "Not run yet"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def from_dict(cls, data):
        fields = {key: data.get(key) for key in cls.__dataclass_fields__}
        job = cls(**{key: value for key, value in fields.items() if value is not None})
        job.interval_minutes = max(1, int(job.interval_minutes or 60))
        job.schedule_unit = SCHEDULE_DAYS if job.schedule_unit == SCHEDULE_DAYS else SCHEDULE_MINUTES
        if "schedule_amount" not in data:
            job.schedule_amount = job.interval_minutes
        job.schedule_amount = max(1, int(job.schedule_amount or 1))
        job.mode = TWO_WAY if job.mode == TWO_WAY else ONE_WAY
        return job

    def schedule_next(self):
        if self.schedule_enabled and self.enabled:
            if self.schedule_unit == SCHEDULE_DAYS:
                delta = timedelta(days=max(1, self.schedule_amount))
            else:
                delta = timedelta(minutes=max(1, self.schedule_amount))
                self.interval_minutes = max(1, self.schedule_amount)
            self.next_run = (datetime.now() + delta).replace(microsecond=0).isoformat()
        else:
            self.next_run = ""


class JobStore:
    def __init__(self, path=JOBS_FILE):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            return [SyncJob.from_dict(item) for item in data.get("jobs", [])]
        except Exception:
            backup = self.path.with_suffix(".broken.json")
            shutil.copy2(self.path, backup)
            return []

    def save(self, jobs):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": APP_VERSION, "jobs": [asdict(job) for job in jobs]}
        with self.path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)


class SyncCancelled(Exception):
    pass


class FolderSyncer:
    def __init__(self, job, log, should_cancel, dry_run_override=None):
        self.job = job
        self.source = Path(job.source).resolve()
        self.destination = Path(job.destination).resolve()
        self.log = log
        self.should_cancel = should_cancel
        self.stats = SyncStats()
        self.dry_run = job.dry_run if dry_run_override is None else dry_run_override

    def run(self):
        self._validate()
        self.destination.mkdir(parents=True, exist_ok=True)

        label = "previewing" if self.dry_run else "syncing"
        self.log(f"{self.job.name}: {label} {self.source} <-> {self.destination}" if self.job.mode == TWO_WAY else f"{self.job.name}: {label} {self.source} -> {self.destination}")

        if self.job.mode == TWO_WAY:
            self._sync_two_way()
        else:
            self._copy_newer_files(self.source, self.destination, "Source")
            if self.job.mirror:
                self._delete_extra_files(self.source, self.destination, "Destination")

        return self.stats

    def _validate(self):
        if not self.job.name.strip():
            raise ValueError("Give this job a name.")
        if not self.source.exists() or not self.source.is_dir():
            raise ValueError("Choose a valid source folder.")

        source_text = str(self.source).lower()
        destination_text = str(self.destination).lower()
        if not destination_text:
            raise ValueError("Choose a destination folder.")
        if source_text == destination_text:
            raise ValueError("Source and destination must be different folders.")
        if destination_text.startswith(source_text + os.sep):
            raise ValueError("Destination cannot be inside the source folder.")
        if source_text.startswith(destination_text + os.sep):
            raise ValueError("Source cannot be inside the destination folder.")

    def _sync_two_way(self):
        all_paths = self._relative_files(self.source) | self._relative_files(self.destination)
        for relative_path in sorted(all_paths, key=str):
            self._check_cancelled()
            left = self.source / relative_path
            right = self.destination / relative_path

            try:
                if left.exists() and right.exists():
                    self._sync_existing_pair(left, right, relative_path)
                elif left.exists():
                    self._copy_file(left, right, "Source")
                    self.stats.copied += 1
                elif right.exists():
                    self._copy_file(right, left, "Destination")
                    self.stats.copied += 1
            except Exception as exc:
                self.stats.errors += 1
                self.log(f"Error: {relative_path} ({exc})")

    def _sync_existing_pair(self, left, right, relative_path):
        left_stat = left.stat()
        right_stat = right.stat()
        same_size = left_stat.st_size == right_stat.st_size
        same_time = abs(left_stat.st_mtime - right_stat.st_mtime) <= TIME_TOLERANCE_SECONDS
        if same_size and same_time:
            self.stats.skipped += 1
            return

        if abs(left_stat.st_mtime - right_stat.st_mtime) <= TIME_TOLERANCE_SECONDS:
            self.stats.conflicts += 1
            self.log(f"Conflict: {relative_path} changed on both sides with the same timestamp")
            return

        if left_stat.st_mtime > right_stat.st_mtime:
            self._copy_file(left, right, "Source")
        else:
            self._copy_file(right, left, "Destination")
        self.stats.copied += 1

    def _copy_newer_files(self, source_root, destination_root, side_name):
        for source_file in source_root.rglob("*"):
            self._check_cancelled()
            if not source_file.is_file():
                continue

            relative_path = source_file.relative_to(source_root)
            destination_file = destination_root / relative_path

            try:
                if self._needs_copy(source_file, destination_file):
                    self._copy_file(source_file, destination_file, side_name)
                    self.stats.copied += 1
                else:
                    self.stats.skipped += 1
            except Exception as exc:
                self.stats.errors += 1
                self.log(f"Error: {relative_path} ({exc})")

    def _delete_extra_files(self, source_root, destination_root, side_name):
        if not destination_root.exists():
            return

        for destination_file in sorted(destination_root.rglob("*"), reverse=True):
            self._check_cancelled()
            relative_path = destination_file.relative_to(destination_root)
            source_match = source_root / relative_path

            if source_match.exists():
                continue

            try:
                if destination_file.is_file() or destination_file.is_symlink():
                    self._delete_file(destination_file, destination_root, side_name)
                    self.stats.deleted += 1
                elif destination_file.is_dir() and not any(destination_file.iterdir()):
                    self._delete_directory(destination_file, destination_root)
            except Exception as exc:
                self.stats.errors += 1
                self.log(f"Error deleting {relative_path} ({exc})")

    def _relative_files(self, root):
        if not root.exists():
            return set()
        return {path.relative_to(root) for path in root.rglob("*") if path.is_file()}

    def _needs_copy(self, source_file, destination_file):
        if not destination_file.exists():
            return True

        source_stat = source_file.stat()
        destination_stat = destination_file.stat()
        size_changed = source_stat.st_size != destination_stat.st_size
        source_newer = source_stat.st_mtime > destination_stat.st_mtime + TIME_TOLERANCE_SECONDS
        return size_changed or source_newer

    def _copy_file(self, source_file, destination_file, side_name):
        relative_label = self._relative_label(source_file)
        self.log(f"Copy from {side_name}: {relative_label}")

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

    def _delete_file(self, destination_file, destination_root, side_name):
        relative_path = destination_file.relative_to(destination_root)
        self.log(f"Delete from {side_name}: {relative_path}")

        if not self.dry_run:
            destination_file.unlink()

    def _delete_directory(self, destination_directory, destination_root):
        relative_path = destination_directory.relative_to(destination_root)
        self.log(f"Remove empty folder: {relative_path}")

        if not self.dry_run:
            destination_directory.rmdir()

    def _relative_label(self, path):
        try:
            return path.relative_to(self.source)
        except ValueError:
            try:
                return path.relative_to(self.destination)
            except ValueError:
                return path.name

    def _check_cancelled(self):
        if self.should_cancel():
            raise SyncCancelled()


class SyncoApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} - Sync Jobs")
        self.root.geometry("1120x720")
        self.root.minsize(980, 640)

        self.store = JobStore()
        self.jobs = self.store.load()
        self.selected_job_id = None

        self.name = StringVar()
        self.source = StringVar()
        self.destination = StringVar()
        self.mode = StringVar(value=ONE_WAY)
        self.mirror = BooleanVar(value=False)
        self.dry_run = BooleanVar(value=False)
        self.enabled = BooleanVar(value=True)
        self.schedule_enabled = BooleanVar(value=False)
        self.interval_minutes = IntVar(value=60)
        self.schedule_unit = StringVar(value=SCHEDULE_MINUTES)
        self.schedule_amount = IntVar(value=60)
        self.status = StringVar(value="Ready.")

        self.messages = queue.Queue()
        self.worker = None
        self.running_job_id = None
        self.cancel_requested = False
        self.loading_form = False
        self.refreshing_jobs = False

        self._configure_style()
        self._build_ui()
        if not self.jobs:
            self._new_job(save=False)
        else:
            self._select_job(self.jobs[0].id)
        self._refresh_jobs()
        self._poll_messages()
        self._schedule_tick()

    def _configure_style(self):
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"))
        style.configure("Hero.TLabelframe", padding=12)
        style.configure("Section.TLabelframe", padding=12)
        style.configure("Accent.TButton", padding=(14, 7))
        style.configure("Action.TButton", padding=(14, 8))
        style.configure("Status.TLabel", padding=(8, 5))

    def _build_ui(self):
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(18, 16, 18, 8))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Professional Windows folder sync manager").grid(row=1, column=0, sticky="w")

        admin = ttk.Frame(header)
        admin.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Button(admin, text="Import", command=self._import_jobs).grid(row=0, column=0, padx=4)
        ttk.Button(admin, text="Export", command=self._export_jobs).grid(row=0, column=1, padx=4)
        ttk.Button(admin, text="Save All", command=self._save_jobs).grid(row=0, column=2, padx=4)

        sidebar = ttk.Frame(self.root, padding=(18, 8, 8, 12))
        sidebar.grid(row=1, column=0, sticky="ns")
        sidebar.rowconfigure(1, weight=1)

        hero = ttk.LabelFrame(sidebar, text="Sync Jobs", style="Hero.TLabelframe")
        hero.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.total_jobs_label = ttk.Label(hero, text="0 jobs", font=("Segoe UI", 15, "bold"))
        self.total_jobs_label.grid(row=0, column=0, sticky="w")
        self.next_run_label = ttk.Label(hero, text="No scheduled jobs")
        self.next_run_label.grid(row=1, column=0, sticky="w", pady=(4, 0))

        jobs_frame = ttk.Frame(sidebar)
        jobs_frame.grid(row=1, column=0, sticky="nsew")
        jobs_frame.rowconfigure(0, weight=1)
        jobs_frame.columnconfigure(0, weight=1)

        self.jobs_list = ttk.Treeview(jobs_frame, columns=("summary",), show="tree headings", height=18)
        self.jobs_list.heading("#0", text="Job")
        self.jobs_list.heading("summary", text="Status")
        self.jobs_list.column("#0", width=190, stretch=False)
        self.jobs_list.column("summary", width=160, stretch=False)
        self.jobs_list.grid(row=0, column=0, sticky="nsew")
        self.jobs_list.bind("<<TreeviewSelect>>", self._job_selected)

        list_scroll = ttk.Scrollbar(jobs_frame, orient="vertical", command=self.jobs_list.yview)
        list_scroll.grid(row=0, column=1, sticky="ns")
        self.jobs_list.configure(yscrollcommand=list_scroll.set)

        job_actions = ttk.Frame(sidebar)
        job_actions.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(job_actions, text="New", command=lambda: self._new_job()).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(job_actions, text="Duplicate", command=self._duplicate_job).grid(row=0, column=1, padx=6)
        ttk.Button(job_actions, text="Delete", command=self._delete_job).grid(row=0, column=2, padx=6)

        main = ttk.Frame(self.root, padding=(8, 8, 18, 12))
        main.grid(row=1, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        cards = ttk.Frame(main)
        cards.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        cards.columnconfigure(0, weight=1)
        cards.columnconfigure(1, weight=1)
        cards.columnconfigure(2, weight=1)

        self.card_mode = self._metric_card(cards, 0, "Mode", ONE_WAY)
        self.card_schedule = self._metric_card(cards, 1, "Schedule", "Manual")
        self.card_last = self._metric_card(cards, 2, "Last Run", "Never")

        editor = ttk.LabelFrame(main, text="Job Editor", style="Section.TLabelframe")
        editor.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        editor.columnconfigure(1, weight=1)

        self._entry_row(editor, 0, "Job name", self.name)
        self._folder_row(editor, 1, "Source", self.source, self._choose_source)
        self._folder_row(editor, 2, "Destination", self.destination, self._choose_destination)

        ttk.Label(editor, text="Sync mode").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=6)
        mode_box = ttk.Combobox(editor, textvariable=self.mode, values=(ONE_WAY, TWO_WAY), state="readonly", width=20)
        mode_box.grid(row=3, column=1, sticky="w", pady=6)
        mode_box.bind("<<ComboboxSelected>>", lambda _event: self._sync_form_to_job())

        options = ttk.Frame(editor)
        options.grid(row=4, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(options, text="Enabled", variable=self.enabled, command=self._sync_form_to_job).grid(row=0, column=0, padx=(0, 22))
        ttk.Checkbutton(options, text="Preview by default", variable=self.dry_run, command=self._sync_form_to_job).grid(row=0, column=1, padx=(0, 22))
        ttk.Checkbutton(options, text="Mirror destination", variable=self.mirror, command=self._sync_form_to_job).grid(row=0, column=2)

        schedule = ttk.Frame(editor)
        schedule.grid(row=5, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(schedule, text="Scheduled sync", variable=self.schedule_enabled, command=self._sync_form_to_job).grid(row=0, column=0, padx=(0, 16))
        ttk.Label(schedule, text="Every").grid(row=0, column=1, padx=(0, 6))
        ttk.Spinbox(schedule, from_=1, to=10080, textvariable=self.schedule_amount, width=8, command=self._sync_form_to_job).grid(row=0, column=2)
        unit_box = ttk.Combobox(schedule, textvariable=self.schedule_unit, values=(SCHEDULE_MINUTES, SCHEDULE_DAYS), state="readonly", width=10)
        unit_box.grid(row=0, column=3, padx=(6, 0))
        unit_box.bind("<<ComboboxSelected>>", lambda _event: self._sync_form_to_job())

        actions = ttk.Frame(editor)
        actions.grid(row=6, column=1, sticky="w", pady=(16, 0))
        button_width = 12
        ttk.Button(actions, text="Save Job", command=self._save_current_job, style="Action.TButton", width=button_width).grid(row=0, column=0, padx=(0, 8), ipady=1)
        ttk.Button(actions, text="Preview", command=lambda: self._start_sync(preview=True), style="Action.TButton", width=button_width).grid(row=0, column=1, padx=8, ipady=1)
        self.run_button = ttk.Button(actions, text="Run Now", command=lambda: self._start_sync(preview=False), style="Action.TButton", width=button_width)
        self.run_button.grid(row=0, column=2, padx=8, ipady=1)
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self._cancel_sync, state="disabled", style="Action.TButton", width=button_width)
        self.cancel_button.grid(row=0, column=3, padx=8, ipady=1)

        log_frame = ttk.LabelFrame(main, text="Activity", style="Section.TLabelframe")
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_box = ttk.Treeview(log_frame, columns=("time", "message"), show="headings")
        self.log_box.heading("time", text="Time")
        self.log_box.heading("message", text="Message")
        self.log_box.column("time", width=90, stretch=False)
        self.log_box.column("message", anchor="w", stretch=True)
        self.log_box.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=scrollbar.set)

        status_bar = ttk.Label(self.root, textvariable=self.status, relief="sunken", anchor="w", style="Status.TLabel")
        status_bar.grid(row=2, column=0, columnspan=2, sticky="ew")

        for variable in (self.name, self.source, self.destination, self.mode):
            variable.trace_add("write", lambda *_args: self._sync_form_to_job())
        for variable in (self.interval_minutes, self.schedule_amount, self.schedule_unit):
            variable.trace_add("write", lambda *_args: self._sync_form_to_job())

    def _metric_card(self, parent, column, title, value):
        card = ttk.LabelFrame(parent, text=title, style="Hero.TLabelframe")
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
        label = ttk.Label(card, text=value, font=("Segoe UI", 13, "bold"))
        label.grid(row=0, column=0, sticky="w")
        return label

    def _entry_row(self, parent, row, label, variable):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=6)

    def _folder_row(self, parent, row, label, variable, command):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=6)
        ttk.Button(parent, text="Browse", command=command).grid(row=row, column=2, sticky="e", padx=(10, 0), pady=6)

    def _choose_source(self):
        folder = filedialog.askdirectory(title="Choose source folder")
        if folder:
            self.source.set(folder)

    def _choose_destination(self):
        folder = filedialog.askdirectory(title="Choose destination folder")
        if folder:
            self.destination.set(folder)

    def _new_job(self, save=True):
        job = SyncJob(name=f"Sync Job {len(self.jobs) + 1}")
        self.jobs.append(job)
        self.selected_job_id = job.id
        if save:
            self._save_jobs()
        self._load_job_to_form(job)
        self._refresh_jobs()

    def _duplicate_job(self):
        job = self._current_job()
        if not job:
            return
        clone = SyncJob.from_dict(asdict(job))
        clone.id = uuid.uuid4().hex
        clone.name = f"{job.name} Copy"
        clone.last_run = ""
        clone.next_run = ""
        clone.last_status = "Not run yet"
        self.jobs.append(clone)
        self.selected_job_id = clone.id
        self._save_jobs()
        self._load_job_to_form(clone)
        self._refresh_jobs()

    def _delete_job(self):
        job = self._current_job()
        if not job:
            return
        if not messagebox.askyesno(APP_NAME, f"Delete '{job.name}'?"):
            return
        self.jobs = [item for item in self.jobs if item.id != job.id]
        self.selected_job_id = self.jobs[0].id if self.jobs else None
        self._save_jobs()
        if self.selected_job_id:
            self._load_job_to_form(self._current_job())
        else:
            self._clear_form()
        self._refresh_jobs()

    def _job_selected(self, _event=None):
        if self.loading_form or self.refreshing_jobs:
            return
        selection = self.jobs_list.selection()
        if not selection:
            return
        if selection[0] == self.selected_job_id:
            return
        self._select_job(selection[0])

    def _select_job(self, job_id):
        self._sync_form_to_job()
        self.selected_job_id = job_id
        job = self._current_job()
        if job:
            self._load_job_to_form(job)
        self._refresh_jobs()

    def _current_job(self):
        for job in self.jobs:
            if job.id == self.selected_job_id:
                return job
        return None

    def _load_job_to_form(self, job):
        self.loading_form = True
        try:
            self.name.set(job.name)
            self.source.set(job.source)
            self.destination.set(job.destination)
            self.mode.set(job.mode)
            self.mirror.set(job.mirror)
            self.dry_run.set(job.dry_run)
            self.enabled.set(job.enabled)
            self.schedule_enabled.set(job.schedule_enabled)
            self.interval_minutes.set(max(1, job.interval_minutes))
            self.schedule_unit.set(job.schedule_unit)
            self.schedule_amount.set(max(1, job.schedule_amount))
            self._refresh_cards(job)
        finally:
            self.loading_form = False

    def _clear_form(self):
        self.name.set("")
        self.source.set("")
        self.destination.set("")
        self.mode.set(ONE_WAY)

    def _sync_form_to_job(self):
        if self.loading_form:
            return
        job = self._current_job()
        if not job:
            return
        try:
            amount = max(1, int(self.schedule_amount.get()))
        except Exception:
            amount = 60
        unit = SCHEDULE_DAYS if self.schedule_unit.get() == SCHEDULE_DAYS else SCHEDULE_MINUTES
        schedule_changed = (
            job.schedule_unit != unit
            or job.schedule_amount != amount
            or job.schedule_enabled != bool(self.schedule_enabled.get())
            or job.enabled != bool(self.enabled.get())
        )
        job.name = self.name.get().strip() or "Untitled Sync Job"
        job.source = self.source.get().strip()
        job.destination = self.destination.get().strip()
        job.mode = TWO_WAY if self.mode.get() == TWO_WAY else ONE_WAY
        job.mirror = bool(self.mirror.get()) and job.mode == ONE_WAY
        job.dry_run = bool(self.dry_run.get())
        job.enabled = bool(self.enabled.get())
        job.schedule_enabled = bool(self.schedule_enabled.get())
        job.schedule_unit = unit
        job.schedule_amount = amount
        job.interval_minutes = amount if unit == SCHEDULE_MINUTES else amount * 1440
        if job.schedule_enabled and (schedule_changed or not parse_iso(job.next_run)):
            job.schedule_next()
        if not job.schedule_enabled:
            job.next_run = ""
        self._refresh_cards(job)
        self._refresh_jobs()

    def _save_current_job(self):
        self._sync_form_to_job()
        self._save_jobs()
        self.status.set("Job saved.")

    def _save_jobs(self):
        self._sync_form_to_job()
        self.store.save(self.jobs)
        self._refresh_jobs()

    def _refresh_jobs(self):
        if self.refreshing_jobs:
            return
        selected = self.selected_job_id
        self.refreshing_jobs = True
        try:
            for item in self.jobs_list.get_children():
                self.jobs_list.delete(item)
            for job in self.jobs:
                markers = []
                if not job.enabled:
                    markers.append("Disabled")
                if job.schedule_enabled:
                    markers.append(schedule_label(job))
                if job.mode == TWO_WAY:
                    markers.append("2-way")
                summary = " | ".join(markers) or "Manual"
                self.jobs_list.insert("", END, iid=job.id, text=job.name, values=(summary,))
            if selected and self.jobs_list.exists(selected):
                current_selection = self.jobs_list.selection()
                if current_selection != (selected,):
                    self.jobs_list.selection_set(selected)

            scheduled = [parse_iso(job.next_run) for job in self.jobs if parse_iso(job.next_run)]
            scheduled = [item for item in scheduled if item]
            self.total_jobs_label.configure(text=f"{len(self.jobs)} job{'s' if len(self.jobs) != 1 else ''}")
            self.next_run_label.configure(text=f"Next: {min(scheduled).strftime('%I:%M %p')}" if scheduled else "No scheduled jobs")
        finally:
            self.refreshing_jobs = False

    def _refresh_cards(self, job):
        schedule = "Manual"
        if job.schedule_enabled:
            next_run = parse_iso(job.next_run)
            schedule = schedule_label(job)
            if next_run:
                schedule += f" | {next_run.strftime('%I:%M %p')}"
        last_run = parse_iso(job.last_run)
        self.card_mode.configure(text=job.mode)
        self.card_schedule.configure(text=schedule)
        self.card_last.configure(text=last_run.strftime("%d %b, %I:%M %p") if last_run else "Never")

    def _start_sync(self, preview=False, scheduled_job=None):
        if self.worker and self.worker.is_alive():
            self.status.set("A sync is already running.")
            return

        if scheduled_job:
            job = scheduled_job
            self.selected_job_id = job.id
            self._load_job_to_form(job)
            dry_run = False
        else:
            self._sync_form_to_job()
            job = self._current_job()
            dry_run = True if preview else None

        if not job:
            return

        self.cancel_requested = False
        self.running_job_id = job.id
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status.set(f"Running {job.name}...")
        self._clear_log()

        syncer = FolderSyncer(job, self._queue_log, lambda: self.cancel_requested, dry_run_override=dry_run)
        self.worker = threading.Thread(target=self._run_sync, args=(syncer, job.id), daemon=True)
        self.worker.start()

    def _run_sync(self, syncer, job_id):
        started_at = time.time()
        try:
            stats = syncer.run()
            elapsed = time.time() - started_at
            summary = (
                f"Done in {elapsed:.1f}s. Copied {stats.copied}, skipped {stats.skipped}, "
                f"deleted {stats.deleted}, conflicts {stats.conflicts}, errors {stats.errors}."
            )
            self.messages.put(("done", job_id, summary))
        except SyncCancelled:
            self.messages.put(("done", job_id, "Sync cancelled."))
        except Exception as exc:
            self.messages.put(("error", job_id, str(exc)))

    def _cancel_sync(self):
        self.cancel_requested = True
        self.status.set("Cancelling...")

    def _queue_log(self, message):
        self.messages.put(("log", "", message))

    def _poll_messages(self):
        while True:
            try:
                kind, job_id, text = self.messages.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._append_log(text)
            elif kind == "done":
                job = self._job_by_id(job_id)
                if job:
                    job.last_run = now_iso()
                    job.last_status = text
                    job.schedule_next()
                    self.store.save(self.jobs)
                self._append_log(text)
                self.status.set(text)
                self._set_idle()
                self._refresh_jobs()
                if job:
                    self._refresh_cards(job)
            elif kind == "error":
                job = self._job_by_id(job_id)
                if job:
                    job.last_run = now_iso()
                    job.last_status = f"Failed: {text}"
                    job.schedule_next()
                    self.store.save(self.jobs)
                self.status.set("Sync failed.")
                self._append_log(f"Error: {text}")
                messagebox.showerror(APP_NAME, text)
                self._set_idle()
                self._refresh_jobs()

        self.root.after(100, self._poll_messages)

    def _schedule_tick(self):
        if not (self.worker and self.worker.is_alive()):
            now = datetime.now()
            for job in self.jobs:
                due = parse_iso(job.next_run)
                if job.enabled and job.schedule_enabled and due and due <= now:
                    self._append_log(f"Scheduled run started: {job.name}")
                    self._start_sync(scheduled_job=job)
                    break
        self.root.after(30000, self._schedule_tick)

    def _job_by_id(self, job_id):
        for job in self.jobs:
            if job.id == job_id:
                return job
        return None

    def _append_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.insert("", END, values=(timestamp, message))
        children = self.log_box.get_children()
        if children:
            self.log_box.see(children[-1])

    def _clear_log(self):
        for item in self.log_box.get_children():
            self.log_box.delete(item)

    def _set_idle(self):
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.running_job_id = None

    def _export_jobs(self):
        self._save_jobs()
        target = filedialog.asksaveasfilename(
            title="Export sync jobs",
            defaultextension=".json",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if not target:
            return
        shutil.copy2(self.store.path, target)
        self.status.set("Jobs exported.")

    def _import_jobs(self):
        source = filedialog.askopenfilename(
            title="Import sync jobs",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if not source:
            return
        imported = JobStore(source).load()
        if not imported:
            messagebox.showinfo(APP_NAME, "No jobs found in that file.")
            return
        for job in imported:
            job.id = uuid.uuid4().hex
        self.jobs.extend(imported)
        self.selected_job_id = imported[0].id
        self._save_jobs()
        self._load_job_to_form(imported[0])
        self._refresh_jobs()
        self.status.set(f"Imported {len(imported)} job(s).")


def main():
    try:
        root = Tk()

        def show_callback_error(exc_type, exc_value, exc_traceback):
            with ERROR_LOG.open("a", encoding="utf-8") as file:
                file.write(f"\n[{now_iso()}] {exc_value}\n")
                traceback.print_exception(exc_type, exc_value, exc_traceback, file=file)
                file.write("\n")
            messagebox.showerror(APP_NAME, f"Something went wrong.\n\nDetails were saved to:\n{ERROR_LOG}")

        root.report_callback_exception = show_callback_error
        SyncoApp(root)
        bring_window_forward(root)
        root.mainloop()
    except Exception as exc:
        log_error(exc)
        try:
            messagebox.showerror(APP_NAME, f"Synco could not start.\n\nDetails were saved to:\n{ERROR_LOG}")
        except Exception:
            print(f"Synco could not start. Details were saved to: {ERROR_LOG}")
        raise


if __name__ == "__main__":
    main()
