#!/usr/bin/env python3
"""Windows GUI launcher for the OT-2 utility scripts in this repo."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from collections import OrderedDict
from typing import Dict, List, Optional

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Tkinter is required to run this launcher.") from exc


if getattr(sys, "frozen", False):
    REPO_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    REPO_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = REPO_DIR

SETTINGS_PATH = os.path.join(APP_DIR, ".windows_script_runner.json")


def _default_python_path():
    candidates = [
        os.path.join(
            os.path.expanduser("~"),
            "AppData",
            "Local",
            "Programs",
            "Opentrons",
            "resources",
            "python",
            "x64",
            "python.exe",
        ),
        sys.executable if not getattr(sys, "frozen", False) else "",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


SCRIPT_DEFINITIONS = OrderedDict(
    [
        (
            "Resolve OT-2 Host",
            {
                "path": "ot2_resolve_host.py",
                "description": "Discover or verify the OT-2 host reachable from this Windows machine.",
                "fields": [
                    {"name": "host", "label": "Host Override", "type": "text", "default": ""},
                    {"name": "port", "label": "API Port", "type": "int", "default": 31950},
                    {"name": "api_version", "label": "API Version", "type": "text", "default": "2"},
                    {"name": "timeout", "label": "Probe Timeout (s)", "type": "float", "default": 2.0},
                    {
                        "name": "pick_first",
                        "label": "Pick First Reachable Host",
                        "type": "bool",
                        "default": False,
                    },
                ],
            },
        ),
        (
            "Ensure SSH Key",
            {
                "path": "ot2_ensure_ssh_key.py",
                "description": "Generate or reuse an SSH key and optionally authorize it on the OT-2.",
                "fields": [
                    {"name": "host", "label": "Host Override", "type": "text", "default": ""},
                    {"name": "api_port", "label": "API Port", "type": "int", "default": 31950},
                    {"name": "api_version", "label": "API Version", "type": "text", "default": "2"},
                    {
                        "name": "health_timeout",
                        "label": "Health Timeout (s)",
                        "type": "float",
                        "default": 2.0,
                    },
                    {"name": "ssh_user", "label": "SSH User", "type": "text", "default": "root"},
                    {"name": "ssh_port", "label": "SSH Port", "type": "int", "default": 22},
                    {"name": "key_dir", "label": "Key Directory", "type": "dir", "default": ""},
                    {
                        "name": "scope",
                        "label": "Key Scope",
                        "type": "choice",
                        "choices": ["per-robot", "shared"],
                        "default": "per-robot",
                    },
                    {
                        "name": "ensure_authorized",
                        "label": "Install Public Key If Needed",
                        "type": "bool",
                        "default": True,
                    },
                ],
            },
        ),
        (
            "Pull Calibrations",
            {
                "path": "ot2_pull_calibrations.py",
                "description": "Download API snapshots and calibration files from a connected OT-2.",
                "fields": [
                    {"name": "host", "label": "Host Override", "type": "text", "default": ""},
                    {"name": "api_port", "label": "API Port", "type": "int", "default": 31950},
                    {"name": "api_version", "label": "API Version", "type": "text", "default": "2"},
                    {"name": "ssh_user", "label": "SSH User", "type": "text", "default": "root"},
                    {"name": "ssh_port", "label": "SSH Port", "type": "int", "default": 22},
                    {"name": "ssh_key", "label": "SSH Private Key", "type": "file", "default": ""},
                    {"name": "out_dir", "label": "Output Folder", "type": "dir", "default": ""},
                    {"name": "api_only", "label": "API Only", "type": "bool", "default": False},
                ],
            },
        ),
        (
            "Pull RPI Offsets",
            {
                "path": "pull_rpi_offsets.py",
                "description": "Pull calibration snapshots over the OT-2 HTTP API into the offsets folder.",
                "fields": [
                    {"name": "host", "label": "Host Override", "type": "text", "default": ""},
                    {"name": "api_port", "label": "API Port", "type": "int", "default": 31950},
                    {"name": "api_version", "label": "API Version", "type": "text", "default": "2"},
                    {"name": "out_root", "label": "Output Root Folder", "type": "dir", "default": ""},
                ],
            },
        ),
        (
            "Apply Standard Offsets",
            {
                "path": "apply_standard_offsets.py",
                "description": "Rewrite standard offset templates to the attached pipettes and upload them.",
                "fields": [
                    {"name": "host", "label": "Host Override", "type": "text", "default": ""},
                    {"name": "api_port", "label": "API Port", "type": "int", "default": 31950},
                    {"name": "api_version", "label": "API Version", "type": "text", "default": "2"},
                    {"name": "ssh_user", "label": "SSH User", "type": "text", "default": "root"},
                    {"name": "ssh_port", "label": "SSH Port", "type": "int", "default": 22},
                    {"name": "ssh_key", "label": "SSH Private Key", "type": "file", "default": ""},
                    {"name": "ssh_key_dir", "label": "SSH Key Dir", "type": "dir", "default": ""},
                    {
                        "name": "ssh_key_scope",
                        "label": "Auto Key Scope",
                        "type": "choice",
                        "choices": ["per-robot", "shared"],
                        "default": "per-robot",
                    },
                    {
                        "name": "ensure_ssh_key",
                        "label": "Auto-Setup SSH Key",
                        "type": "bool",
                        "default": True,
                    },
                    {
                        "name": "no_ensure_ssh_key",
                        "label": "Disable Auto SSH Setup",
                        "type": "bool",
                        "default": False,
                    },
                    {"name": "dry_run", "label": "Dry Run", "type": "bool", "default": False},
                    {"name": "remote_tag", "label": "Remote Tag", "type": "text", "default": "standard-offsets-upload"},
                    {
                        "name": "restart_robot_server",
                        "label": "Restart Robot Server",
                        "type": "bool",
                        "default": False,
                    },
                    {
                        "name": "restart_wait_seconds",
                        "label": "Restart Wait (s)",
                        "type": "float",
                        "default": 120.0,
                    },
                    {"name": "offsets_dir", "label": "Offsets Dir", "type": "dir", "default": os.path.join(REPO_DIR, "offsets")},
                    {
                        "name": "pipette_offsets_template",
                        "label": "Pipette Template",
                        "type": "text",
                        "default": "pipette_offsets_all.json",
                    },
                    {
                        "name": "tip_length_template",
                        "label": "Tip Length Template",
                        "type": "text",
                        "default": "tip_length_offsets_all.json",
                    },
                    {
                        "name": "deck_template",
                        "label": "Deck Template",
                        "type": "text",
                        "default": "calibration_status_with_deck_offset.json",
                    },
                ],
            },
        ),
    ]
)


class ScriptRunnerApp(object):
    def __init__(self, root):
        self.root = root
        self.root.title("OT-2 Windows Script Runner")
        self.root.geometry("1180x760")
        self.root.minsize(980, 680)

        self._process = None  # type: Optional[subprocess.Popen]
        self._reader_thread = None  # type: Optional[threading.Thread]
        self._output_queue = queue.Queue()  # type: queue.Queue
        self._field_vars = {}  # type: Dict[str, object]
        self._field_widgets = {}  # type: Dict[str, object]
        self._settings = self._load_settings()

        self.python_var = tk.StringVar(value=self._settings.get("python_path", _default_python_path()))
        default_script = self._settings.get("selected_script") or next(iter(SCRIPT_DEFINITIONS.keys()))
        if default_script not in SCRIPT_DEFINITIONS:
            default_script = next(iter(SCRIPT_DEFINITIONS.keys()))
        self.script_var = tk.StringVar(value=default_script)
        self.status_var = tk.StringVar(value="Ready.")
        self.command_var = tk.StringVar(value="")

        self._build_ui()
        self._render_form()
        self.root.after(150, self._drain_output_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=0)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        control_panel = ttk.Frame(outer)
        control_panel.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        control_panel.columnconfigure(0, weight=1)

        script_group = ttk.LabelFrame(control_panel, text="Script", padding=10)
        script_group.grid(row=0, column=0, sticky="ew")
        script_group.columnconfigure(0, weight=1)

        script_names = list(SCRIPT_DEFINITIONS.keys())
        script_combo = ttk.Combobox(
            script_group,
            textvariable=self.script_var,
            values=script_names,
            state="readonly",
            width=34,
        )
        script_combo.grid(row=0, column=0, sticky="ew")
        script_combo.bind("<<ComboboxSelected>>", self._on_script_changed)

        self.description_label = ttk.Label(
            script_group,
            text="",
            wraplength=300,
            justify=tk.LEFT,
        )
        self.description_label.grid(row=1, column=0, sticky="w", pady=(8, 0))

        python_group = ttk.LabelFrame(control_panel, text="Python", padding=10)
        python_group.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        python_group.columnconfigure(0, weight=1)

        python_entry = ttk.Entry(python_group, textvariable=self.python_var)
        python_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(python_group, text="Browse...", command=self._browse_python).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Button(python_group, text="Check", command=self._check_python).grid(
            row=1, column=1, padx=(8, 0), pady=(8, 0)
        )
        ttk.Label(
            python_group,
            text="Select a Python 3.10+ interpreter used to run the repo scripts.",
            wraplength=300,
            justify=tk.LEFT,
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))

        form_group = ttk.LabelFrame(control_panel, text="Parameters", padding=10)
        form_group.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        form_group.columnconfigure(0, weight=1)
        control_panel.rowconfigure(2, weight=1)

        self.form_frame = ttk.Frame(form_group)
        self.form_frame.grid(row=0, column=0, sticky="nsew")
        form_group.rowconfigure(0, weight=1)

        action_group = ttk.Frame(control_panel)
        action_group.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        action_group.columnconfigure(0, weight=1)
        action_group.columnconfigure(1, weight=1)
        action_group.columnconfigure(2, weight=1)

        self.run_button = ttk.Button(action_group, text="Run Script", command=self._run_script)
        self.run_button.grid(row=0, column=0, sticky="ew")
        self.stop_button = ttk.Button(action_group, text="Stop", command=self._stop_script, state=tk.DISABLED)
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(action_group, text="Clear Output", command=self._clear_output).grid(row=0, column=2, sticky="ew")

        output_panel = ttk.Frame(outer)
        output_panel.grid(row=0, column=1, sticky="nsew")
        output_panel.columnconfigure(0, weight=1)
        output_panel.rowconfigure(1, weight=1)

        status_group = ttk.LabelFrame(output_panel, text="Run Status", padding=10)
        status_group.grid(row=0, column=0, sticky="ew")
        status_group.columnconfigure(0, weight=1)

        ttk.Label(status_group, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        ttk.Label(
            status_group,
            textvariable=self.command_var,
            wraplength=760,
            justify=tk.LEFT,
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))

        console_group = ttk.LabelFrame(output_panel, text="Output", padding=10)
        console_group.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        console_group.columnconfigure(0, weight=1)
        console_group.rowconfigure(0, weight=1)

        self.output_text = tk.Text(console_group, wrap="word", state="disabled", height=30)
        self.output_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(console_group, orient="vertical", command=self.output_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.output_text.configure(yscrollcommand=scrollbar.set)

    def _render_form(self):
        for child in self.form_frame.winfo_children():
            child.destroy()

        self._field_vars = {}
        self._field_widgets = {}

        script_name = self.script_var.get()
        config = SCRIPT_DEFINITIONS[script_name]
        self.description_label.configure(text=config["description"])

        saved = self._settings.get("scripts", {}).get(script_name, {})

        for row_index, field in enumerate(config["fields"]):
            field_name = field["name"]
            field_type = field["type"]
            initial = saved.get(field_name, field.get("default"))

            ttk.Label(self.form_frame, text=field["label"]).grid(
                row=row_index,
                column=0,
                sticky="w",
                pady=4,
            )

            if field_type == "bool":
                var = tk.BooleanVar(value=bool(initial))
                widget = ttk.Checkbutton(self.form_frame, variable=var)
                widget.grid(row=row_index, column=1, sticky="w", pady=4, padx=(10, 0))
            elif field_type == "choice":
                var = tk.StringVar(value=str(initial))
                widget = ttk.Combobox(
                    self.form_frame,
                    textvariable=var,
                    values=field["choices"],
                    state="readonly",
                    width=28,
                )
                widget.grid(row=row_index, column=1, sticky="ew", pady=4, padx=(10, 0))
            else:
                var = tk.StringVar(value="" if initial is None else str(initial))
                widget = ttk.Entry(self.form_frame, textvariable=var, width=34)
                widget.grid(row=row_index, column=1, sticky="ew", pady=4, padx=(10, 0))
                if field_type in ("file", "dir"):
                    ttk.Button(
                        self.form_frame,
                        text="Browse...",
                        command=lambda n=field_name: self._browse_path(n),
                    ).grid(row=row_index, column=2, sticky="w", padx=(8, 0))

            self._field_vars[field_name] = var
            self._field_widgets[field_name] = widget
            self._attach_var_listener(var)

        self.form_frame.columnconfigure(1, weight=1)
        self._update_command_preview()

    def _browse_python(self):
        selected = filedialog.askopenfilename(
            title="Select Python Interpreter",
            filetypes=[("Python", "*.exe"), ("Executable", "*.exe"), ("All Files", "*.*")],
        )
        if selected:
            self.python_var.set(selected)
            self._save_settings()
            self._update_command_preview()

    def _browse_path(self, field_name):
        script_name = self.script_var.get()
        field = None
        for item in SCRIPT_DEFINITIONS[script_name]["fields"]:
            if item["name"] == field_name:
                field = item
                break
        if field is None:
            return

        if field["type"] == "dir":
            selected = filedialog.askdirectory(title="Select Folder")
        else:
            selected = filedialog.askopenfilename(title="Select File")

        if selected:
            var = self._field_vars[field_name]
            var.set(selected)
            self._save_settings()
            self._update_command_preview()

    def _check_python(self):
        python_path = self.python_var.get().strip()
        if not python_path:
            messagebox.showerror("Python Required", "Select a Python 3.10+ interpreter first.")
            return
        try:
            proc = subprocess.run(
                [python_path, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        except Exception as exc:
            messagebox.showerror("Python Check Failed", str(exc))
            return

        output = (proc.stdout or "").strip()
        version_tuple = self._parse_version(output)
        if version_tuple is None:
            messagebox.showwarning("Python Check", "Unable to parse interpreter version:\n\n{0}".format(output))
            return
        if version_tuple < (3, 10):
            messagebox.showwarning(
                "Python Too Old",
                "The selected interpreter is {0}. These repo scripts need Python 3.10 or newer.".format(output),
            )
            return
        messagebox.showinfo("Python Check", "Interpreter looks good:\n\n{0}".format(output))

    def _parse_version(self, output):
        parts = output.strip().split()
        if len(parts) < 2:
            return None
        version_text = parts[-1]
        bits = version_text.split(".")
        if len(bits) < 2:
            return None
        try:
            return int(bits[0]), int(bits[1])
        except ValueError:
            return None

    def _attach_var_listener(self, var):
        try:
            var.trace_add("write", self._on_form_value_changed)
        except AttributeError:
            var.trace("w", self._on_form_value_changed)

    def _on_form_value_changed(self, *_args):
        self._save_settings()
        self._update_command_preview()

    def _on_script_changed(self, _event=None):
        self._render_form()
        self._save_settings()

    def _collect_args(self):
        script_name = self.script_var.get()
        config = SCRIPT_DEFINITIONS[script_name]
        args = []

        for field in config["fields"]:
            name = field["name"]
            flag = "--" + name.replace("_", "-")
            value = self._field_vars[name].get()
            if field["type"] == "bool":
                if bool(value):
                    args.append(flag)
            else:
                text = str(value).strip()
                if text:
                    args.extend([flag, text])
        return args

    def _build_command(self):
        python_path = self.python_var.get().strip()
        script_name = self.script_var.get()
        if not python_path:
            return None
        script_path = os.path.join(REPO_DIR, SCRIPT_DEFINITIONS[script_name]["path"])
        return [python_path, script_path] + self._collect_args()

    def _quote(self, value):
        if not value:
            return '""'
        if " " in value or "\t" in value or '"' in value:
            return '"' + value.replace('"', '\\"') + '"'
        return value

    def _update_command_preview(self):
        command = self._build_command()
        if command:
            self.command_var.set("Command: " + " ".join(self._quote(part) for part in command))
        else:
            self.command_var.set("Command: Select a Python 3.10+ interpreter to enable runs.")

    def _append_output(self, text):
        self.output_text.configure(state="normal")
        self.output_text.insert(tk.END, text)
        self.output_text.see(tk.END)
        self.output_text.configure(state="disabled")

    def _clear_output(self):
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", tk.END)
        self.output_text.configure(state="disabled")

    def _run_script(self):
        if self._process is not None:
            messagebox.showinfo("Already Running", "Wait for the current script to finish or stop it first.")
            return

        python_path = self.python_var.get().strip()
        if not python_path:
            messagebox.showerror("Python Required", "Select a Python 3.10+ interpreter first.")
            return

        if not os.path.exists(python_path):
            messagebox.showerror("Missing Interpreter", "The selected Python executable was not found.")
            return

        command = self._build_command()
        self._update_command_preview()
        self._save_settings()

        try:
            self._process = subprocess.Popen(
                command,
                cwd=REPO_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
        except Exception as exc:
            self._process = None
            messagebox.showerror("Launch Failed", str(exc))
            return

        self.run_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.status_var.set("Running {0}...".format(self.script_var.get()))
        self._append_output("\n=== Running {0} ===\n".format(self.script_var.get()))

        self._reader_thread = threading.Thread(target=self._read_process_output, daemon=True)
        self._reader_thread.start()

    def _read_process_output(self):
        proc = self._process
        if proc is None or proc.stdout is None:
            return

        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            self._output_queue.put(("text", line))

        return_code = proc.wait()
        self._output_queue.put(("done", return_code))

    def _drain_output_queue(self):
        try:
            while True:
                event_type, payload = self._output_queue.get_nowait()
                if event_type == "text":
                    self._append_output(payload)
                elif event_type == "done":
                    self._finalize_run(payload)
        except queue.Empty:
            pass
        finally:
            self.root.after(150, self._drain_output_queue)

    def _finalize_run(self, return_code):
        script_name = self.script_var.get()
        self._append_output("\n=== Exit code: {0} ===\n".format(return_code))
        self.status_var.set(
            "{0} finished successfully." .format(script_name)
            if return_code == 0
            else "{0} failed with exit code {1}.".format(script_name, return_code)
        )
        self._process = None
        self.run_button.configure(state=tk.NORMAL)
        self.stop_button.configure(state=tk.DISABLED)

    def _stop_script(self):
        proc = self._process
        if proc is None:
            return
        try:
            proc.terminate()
            self.status_var.set("Stopping script...")
        except Exception as exc:
            messagebox.showerror("Stop Failed", str(exc))

    def _load_settings(self):
        if not os.path.isfile(SETTINGS_PATH):
            return {}
        try:
            with open(SETTINGS_PATH, "r") as handle:
                return json.load(handle)
        except Exception:
            return {}

    def _save_settings(self):
        scripts = self._settings.setdefault("scripts", {})
        script_name = self.script_var.get()
        script_settings = {}
        for key, var in self._field_vars.items():
            script_settings[key] = var.get()
        scripts[script_name] = script_settings
        self._settings["selected_script"] = script_name
        self._settings["python_path"] = self.python_var.get().strip()

        try:
            with open(SETTINGS_PATH, "w") as handle:
                json.dump(self._settings, handle, indent=2)
        except Exception:
            pass

    def _on_close(self):
        if self._process is not None:
            if not messagebox.askyesno("Quit", "A script is still running. Close the launcher anyway?"):
                return
            try:
                self._process.terminate()
            except Exception:
                pass
        self._save_settings()
        self.root.destroy()


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("vista")
    except Exception:
        pass
    app = ScriptRunnerApp(root)

    def on_var_changed(*_args):
        app._save_settings()
        app._update_command_preview()

    app._attach_var_listener(app.python_var)
    app._attach_var_listener(app.script_var)
    root.mainloop()


if __name__ == "__main__":
    main()
