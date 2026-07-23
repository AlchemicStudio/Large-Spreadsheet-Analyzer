"""The five wizard step frames and the CSV import dialog.

Views stay thin: they render presenter state and forward user input to the
presenters in :mod:`lsa.gui.presenters`.  All long-running work happens in
:class:`lsa.gui.worker.ScanController`; views only poll its queue via
``after()`` (tkinter widgets must never be touched from a worker thread).
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING

import customtkinter as ctk

from lsa.core.csv_stream import CsvOptions
from lsa.gui.presenters import (
    ConditionDraft,
    FileStepPresenter,
    ImportPresenter,
    ReportPresenter,
    RulesPresenter,
    empty_tokens_to_text,
    parse_empty_tokens,
)
from lsa.gui.widgets import Table

if TYPE_CHECKING:
    from lsa.gui.app import App

_PAD = {"padx": 12, "pady": 6}
_FILETYPES_OPEN = [("Spreadsheets", "*.csv *.xls *.xlsx *.ods *.xlsm"), ("All files", "*.*")]
_SEPARATOR_CHOICES = {",": ",", ";": ";", "Tab": "\t", "|": "|"}
_ENCODING_CHOICES = ["utf-8", "utf-8-sig", "cp1252", "latin-1", "utf-16", "iso8859-15"]


def _show_error(app: App, message: str) -> None:
    messagebox.showerror(app.tr("error.title"), message, parent=app)


class StepFrame(ctk.CTkFrame):
    """Base class for wizard steps."""

    help_key = ""

    def __init__(self, app: App):
        super().__init__(app.body, fg_color="transparent")
        self.app = app
        self.tr = app.tr
        self.grid_columnconfigure(0, weight=1)
        if self.help_key:
            help_label = ctk.CTkLabel(
                self,
                text=self.tr(self.help_key),
                wraplength=760,
                justify="left",
                text_color=("gray25", "gray70"),
            )
            help_label.grid(row=0, column=0, sticky="w", **_PAD)

    def on_next(self) -> bool:
        """Validate/commit before leaving to the next step."""
        return True

    def on_show(self) -> None:
        """Called after the frame is displayed."""


# ------------------------------------------------------------------ step 1: file


class FileStep(StepFrame):
    help_key = "step.file.help"

    def __init__(self, app: App):
        super().__init__(app)
        self.presenter = FileStepPresenter(app.wizard, app.tr)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew", **_PAD)
        row.grid_columnconfigure(0, weight=1)
        self.path_var = ctk.StringVar(value=str(app.wizard.file or ""))
        self.entry = ctk.CTkEntry(
            row, textvariable=self.path_var, placeholder_text=self.tr("file.placeholder")
        )
        self.entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(row, text=self.tr("file.browse"), width=130, command=self._browse).grid(
            row=0, column=1
        )
        self.info = ctk.CTkLabel(self, text=self.presenter.file_info(), anchor="w")
        self.info.grid(row=2, column=0, sticky="w", **_PAD)
        self.warning = ctk.CTkLabel(
            self,
            text=self.tr("file.memory_warning"),
            wraplength=760,
            justify="left",
            text_color=("#8a5a00", "#ffb86b"),
        )
        self._update_warning()

    def _browse(self) -> None:
        path = filedialog.askopenfilename(parent=self.app, filetypes=_FILETYPES_OPEN)
        if path:
            self.path_var.set(path)
            self._apply(path)

    def _apply(self, path: str) -> str | None:
        error = self.presenter.select_file(path)
        if error is None:
            self.info.configure(text=self.presenter.file_info())
            self._update_warning()
        return error

    def _update_warning(self) -> None:
        if self.app.wizard.memory_warning:
            self.warning.grid(row=3, column=0, sticky="w", **_PAD)
        else:
            self.warning.grid_forget()

    def on_next(self) -> bool:
        path = self.path_var.get().strip()
        if not path:
            _show_error(self.app, self.tr("file.invalid", error=self.tr("file.placeholder")))
            return False
        current = self.app.wizard.file
        if current is None or str(current) != path:
            error = self._apply(path)
            if error is not None:
                _show_error(self.app, error)
                return False
        return True


# ------------------------------------------------------------------ step 2: import options


class ImportStep(StepFrame):
    help_key = "step.import.help"

    def __init__(self, app: App):
        super().__init__(app)
        self.presenter = ImportPresenter(app.wizard, app.tr)
        if self.presenter.is_csv:
            self._build_csv()
        else:
            self._build_sheet_picker()

    def _build_csv(self) -> None:
        self.summary = ctk.CTkLabel(self, text=self._csv_summary(), anchor="w", justify="left")
        self.summary.grid(row=1, column=0, sticky="w", **_PAD)
        ctk.CTkButton(
            self, text=self.tr("csv.configure"), width=220, command=self._open_dialog
        ).grid(row=2, column=0, sticky="w", **_PAD)

    def _csv_summary(self) -> str:
        options = self.presenter.csv_options()
        separator = {v: k for k, v in _SEPARATOR_CHOICES.items()}.get(
            options.separator, options.separator
        )
        header = self.tr("csv.has_header") if options.has_header else ""
        tokens = empty_tokens_to_text(self.app.wizard.settings.empty_tokens)
        parts = [
            f"{self.tr('csv.separator')}: {separator}",
            f"{self.tr('csv.quotechar')}: {options.quotechar}",
            f"{self.tr('csv.encoding')}: {options.encoding}",
        ]
        if header:
            parts.append(header)
        if tokens:
            parts.append(f"{self.tr('csv.empty_tokens')}: {tokens}")
        return "\n".join(parts)

    def _open_dialog(self) -> None:
        CsvImportDialog(self.app, self.presenter, on_applied=self._refresh_summary)

    def _refresh_summary(self) -> None:
        self.summary.configure(text=self._csv_summary())

    def _build_sheet_picker(self) -> None:
        wizard = self.app.wizard
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=1, column=0, sticky="w", **_PAD)
        ctk.CTkLabel(row, text=self.tr("sheet.label")).grid(row=0, column=0, padx=(0, 8))
        self.sheet_var = ctk.StringVar(value=wizard.sheet or "")
        ctk.CTkOptionMenu(
            row,
            values=wizard.sheets or [""],
            variable=self.sheet_var,
            command=self.presenter.set_sheet,
            width=240,
        ).grid(row=0, column=1)
        self.header_var = ctk.BooleanVar(value=wizard.has_header)
        ctk.CTkCheckBox(
            self,
            text=self.tr("header.label"),
            variable=self.header_var,
            command=lambda: self.presenter.set_has_header(bool(self.header_var.get())),
        ).grid(row=2, column=0, sticky="w", **_PAD)


class CsvImportDialog(ctk.CTkToplevel):
    """Modal CSV dialect dialog with a live preview of the first rows."""

    def __init__(self, app: App, presenter: ImportPresenter, on_applied) -> None:
        super().__init__(app)
        self.app = app
        self.tr = app.tr
        self.presenter = presenter
        self.on_applied = on_applied
        self.title(self.tr("csv.dialog.title"))
        self.geometry("860x560")
        self.transient(app)
        # grab_set right away can fail before the window is mapped (CTk re-applies
        # attributes shortly after creation) - delay it.
        self._grab_job: str | None = self.after(150, self._grab)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        options = presenter.csv_options()
        form = ctk.CTkFrame(self, fg_color="transparent")
        form.grid(row=0, column=0, sticky="ew", **_PAD)
        separator_display = {v: k for k, v in _SEPARATOR_CHOICES.items()}.get(
            options.separator, options.separator
        )
        self.separator_var = ctk.StringVar(value=separator_display)
        self.quote_var = ctk.StringVar(value=options.quotechar)
        self.encoding_var = ctk.StringVar(value=options.encoding)
        self.header_var = ctk.BooleanVar(value=options.has_header)
        self.tokens_var = ctk.StringVar(
            value=empty_tokens_to_text(app.wizard.settings.empty_tokens)
        )

        def add_field(column: int, key: str, widget: ctk.CTkBaseClass) -> None:
            ctk.CTkLabel(form, text=self.tr(key)).grid(
                row=0, column=column, padx=(0, 4), sticky="w"
            )
            widget.grid(row=1, column=column, padx=(0, 12), sticky="w")

        add_field(
            0,
            "csv.separator",
            ctk.CTkComboBox(
                form,
                values=list(_SEPARATOR_CHOICES),
                variable=self.separator_var,
                width=90,
                command=lambda _v: self._schedule_preview(),
            ),
        )
        add_field(1, "csv.quotechar", ctk.CTkEntry(form, textvariable=self.quote_var, width=60))
        add_field(
            2,
            "csv.encoding",
            ctk.CTkComboBox(
                form,
                values=[options.encoding, *_ENCODING_CHOICES],
                variable=self.encoding_var,
                width=140,
                command=lambda _v: self._schedule_preview(),
            ),
        )
        ctk.CTkCheckBox(
            form,
            text=self.tr("csv.has_header"),
            variable=self.header_var,
            command=self._schedule_preview,
        ).grid(row=1, column=3, padx=(0, 12))

        tokens_row = ctk.CTkFrame(self, fg_color="transparent")
        tokens_row.grid(row=1, column=0, sticky="ew", **_PAD)
        tokens_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tokens_row, text=self.tr("csv.empty_tokens")).grid(
            row=0, column=0, padx=(0, 8)
        )
        ctk.CTkEntry(tokens_row, textvariable=self.tokens_var).grid(row=0, column=1, sticky="ew")

        self.preview_label = ctk.CTkLabel(self, text=self.tr("csv.preview", count=20), anchor="w")
        self.preview_label.grid(row=2, column=0, sticky="w", **_PAD)
        self.table = Table(self, height=12)
        self.table.grid(row=3, column=0, sticky="nsew", **_PAD)
        self.error_label = ctk.CTkLabel(self, text="", text_color=("#a40000", "#ff6b6b"))
        self.error_label.grid(row=4, column=0, sticky="w", **_PAD)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=5, column=0, sticky="e", **_PAD)
        ctk.CTkButton(buttons, text=self.tr("csv.cancel"), width=110, command=self.destroy).grid(
            row=0, column=0, padx=(0, 8)
        )
        ctk.CTkButton(buttons, text=self.tr("csv.apply"), width=110, command=self._apply).grid(
            row=0, column=1
        )

        for var in (self.separator_var, self.quote_var, self.encoding_var, self.tokens_var):
            var.trace_add("write", lambda *_a: self._schedule_preview())
        self._preview_job: str | None = None
        self._refresh_preview()

    def _grab(self) -> None:
        with contextlib.suppress(Exception):  # window may already be closed
            self.grab_set()

    def _current_options(self) -> CsvOptions:
        separator = _SEPARATOR_CHOICES.get(self.separator_var.get(), self.separator_var.get())
        return CsvOptions(
            separator=separator,
            quotechar=self.quote_var.get() or '"',
            encoding=self.encoding_var.get().strip() or "utf-8",
            has_header=bool(self.header_var.get()),
        )

    def _schedule_preview(self) -> None:
        if self._preview_job is not None:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(300, self._refresh_preview)

    def _refresh_preview(self) -> None:
        self._preview_job = None
        rows, error = self.presenter.csv_preview(self._current_options())
        self.error_label.configure(text=error or "")
        width = max((len(r) for r in rows), default=0)
        from lsa.core.columns import index_to_letter

        columns = [index_to_letter(i) for i in range(width)]
        self.table.set_data(columns, rows)

    def _apply(self) -> None:
        options = self._current_options()
        error = self.presenter.apply_csv_options(
            separator=options.separator,
            quotechar=options.quotechar,
            encoding=options.encoding,
            has_header=options.has_header,
            empty_tokens_text=self.tokens_var.get(),
        )
        if error is not None:
            self.error_label.configure(text=error)
            return
        self.on_applied()
        self.destroy()

    def destroy(self) -> None:
        # Cancel pending after() jobs so they don't fire on a dead window.
        for job_attr in ("_preview_job", "_grab_job"):
            job = getattr(self, job_attr, None)
            if job is not None:
                with contextlib.suppress(Exception):
                    self.after_cancel(job)
                setattr(self, job_attr, None)
        super().destroy()


# ------------------------------------------------------------------ step 3: rules


class RulesStep(StepFrame):
    help_key = "step.rules.help"

    def __init__(self, app: App):
        super().__init__(app)
        self.presenter = RulesPresenter(app.wizard, app.tr)
        self.grid_rowconfigure(2, weight=1)

        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", **_PAD)
        ctk.CTkButton(toolbar, text=self.tr("rules.add"), command=self._add_rule).grid(
            row=0, column=0, padx=(0, 8)
        )
        ctk.CTkButton(toolbar, text=self.tr("rules.load"), command=self._load).grid(
            row=0, column=1, padx=(0, 8)
        )
        ctk.CTkButton(toolbar, text=self.tr("rules.save"), command=self._save).grid(row=0, column=2)

        self.rules_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.rules_frame.grid(row=2, column=0, sticky="nsew", **_PAD)
        self.rules_frame.grid_columnconfigure(0, weight=1)

        settings = ctk.CTkFrame(self, fg_color="transparent")
        settings.grid(row=3, column=0, sticky="ew", **_PAD)
        settings.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(settings, text=self.tr("settings.title")).grid(row=0, column=0, padx=(0, 12))
        self.trim_var = ctk.BooleanVar(value=app.wizard.settings.trim)
        ctk.CTkCheckBox(
            settings,
            text=self.tr("settings.trim"),
            variable=self.trim_var,
            command=lambda: self.presenter.set_trim(bool(self.trim_var.get())),
        ).grid(row=0, column=1, padx=(0, 12))
        self.case_var = ctk.BooleanVar(value=app.wizard.settings.case_sensitive)
        ctk.CTkCheckBox(
            settings,
            text=self.tr("settings.case_sensitive"),
            variable=self.case_var,
            command=lambda: self.presenter.set_case_sensitive(bool(self.case_var.get())),
        ).grid(row=0, column=2, padx=(0, 12))
        ctk.CTkLabel(settings, text=self.tr("csv.empty_tokens")).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        self.tokens_var = ctk.StringVar(
            value=empty_tokens_to_text(app.wizard.settings.empty_tokens)
        )
        tokens_entry = ctk.CTkEntry(settings, textvariable=self.tokens_var, width=280)
        tokens_entry.grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))
        self.tokens_var.trace_add("write", lambda *_a: self._tokens_changed())

        self._rebuild_rules()

    def _tokens_changed(self) -> None:
        wizard = self.app.wizard
        wizard.settings = replace(
            wizard.settings, empty_tokens=parse_empty_tokens(self.tokens_var.get())
        )

    def _rebuild_rules(self) -> None:
        for child in self.rules_frame.winfo_children():
            child.destroy()
        drafts = self.presenter.drafts
        if not drafts:
            ctk.CTkLabel(self.rules_frame, text=self.tr("rules.empty")).grid(
                row=0, column=0, **_PAD
            )
            return
        for index, draft in enumerate(drafts):
            self._build_rule_card(index, draft)

    def _build_rule_card(self, index: int, draft) -> None:
        card = ctk.CTkFrame(self.rules_frame)
        card.grid(row=index, column=0, sticky="ew", pady=6)
        card.grid_columnconfigure(1, weight=1)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", **_PAD)
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text=draft.id).grid(row=0, column=0, padx=(0, 10))
        label_var = ctk.StringVar(value=draft.label)

        def set_label(*_a: object, d=draft, v=label_var) -> None:
            d.label = v.get()

        label_var.trace_add("write", set_label)
        entry = ctk.CTkEntry(
            header, textvariable=label_var, placeholder_text=self.tr("rules.rule_label")
        )
        entry.grid(row=0, column=1, sticky="ew", padx=(0, 10))

        match_labels = {"all": self.tr("match.all.short"), "any": self.tr("match.any.short")}
        match_var = ctk.StringVar(value=match_labels[draft.match])
        match_menu = ctk.CTkSegmentedButton(
            header,
            values=list(match_labels.values()),
            variable=match_var,
            command=lambda label, d=draft, m=match_labels: setattr(
                d, "match", {v: k for k, v in m.items()}[label]
            ),
        )
        match_menu.grid(row=0, column=2, padx=(0, 10))
        ctk.CTkButton(
            header,
            text=self.tr("rules.remove"),
            width=120,
            fg_color=("#b3261e", "#7a1f1a"),
            hover_color=("#8c1d18", "#5c1713"),
            command=lambda i=index: self._remove_rule(i),
        ).grid(row=0, column=3)

        for cond_index, cond in enumerate(draft.conditions):
            self._build_condition_row(card, index, cond_index, cond)
        ctk.CTkButton(
            card,
            text=self.tr("rules.condition.add"),
            width=160,
            command=lambda i=index: self._add_condition(i),
        ).grid(row=1 + len(draft.conditions), column=0, sticky="w", **_PAD)

    def _build_condition_row(
        self, card: ctk.CTkFrame, rule_index: int, cond_index: int, cond: ConditionDraft
    ) -> None:
        tr = self.tr
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.grid(row=1 + cond_index, column=0, columnspan=2, sticky="ew", padx=12, pady=2)

        type_labels = {
            "is_empty": tr("cond.type.is_empty"),
            "not_empty": tr("cond.type.not_empty"),
            "equals_column": tr("cond.type.equals_column"),
        }
        by_labels = {
            "letter": tr("col.by.letter"),
            "index": tr("col.by.index"),
            "header": tr("col.by.header"),
        }

        def ref_editor(column: int, by_attr: str, value_attr: str) -> None:
            by_var = ctk.StringVar(value=by_labels[getattr(cond, by_attr)])

            def set_by(label: str, c=cond, attr=by_attr) -> None:
                setattr(c, attr, {v: k for k, v in by_labels.items()}[label])

            ctk.CTkOptionMenu(
                row, values=list(by_labels.values()), variable=by_var, command=set_by, width=130
            ).grid(row=0, column=column, padx=(0, 6))
            value_var = ctk.StringVar(value=getattr(cond, value_attr))

            def set_value(*_a: object, c=cond, attr=value_attr, v=value_var) -> None:
                setattr(c, attr, v.get())

            value_var.trace_add("write", set_value)
            ctk.CTkEntry(
                row, textvariable=value_var, width=140, placeholder_text=tr("col.value")
            ).grid(row=0, column=column + 1, padx=(0, 12))

        ref_editor(0, "column_by", "column_value")

        type_var = ctk.StringVar(value=type_labels[cond.type])

        def set_type(label: str, c=cond) -> None:
            c.type = {v: k for k, v in type_labels.items()}[label]
            self._rebuild_rules()  # equals_column shows/hides the second column

        ctk.CTkOptionMenu(
            row, values=list(type_labels.values()), variable=type_var, command=set_type, width=150
        ).grid(row=0, column=2, padx=(0, 12))

        if cond.type == "equals_column":
            ref_editor(3, "other_by", "other_value")

        ctk.CTkButton(
            row,
            text=tr("rules.condition.remove"),
            width=90,
            command=lambda r=rule_index, c=cond_index: self._remove_condition(r, c),
        ).grid(row=0, column=5, padx=(6, 0))

    def _add_rule(self) -> None:
        self.presenter.add_rule()
        self._rebuild_rules()

    def _remove_rule(self, index: int) -> None:
        self.presenter.remove_rule(index)
        self._rebuild_rules()

    def _add_condition(self, rule_index: int) -> None:
        self.presenter.add_condition(rule_index)
        self._rebuild_rules()

    def _remove_condition(self, rule_index: int, cond_index: int) -> None:
        self.presenter.remove_condition(rule_index, cond_index)
        self._rebuild_rules()

    def _load(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.app, filetypes=[("Rules JSON", "*.json"), ("All files", "*.*")]
        )
        if not path:
            return
        error = self.presenter.load(path)
        if error is not None:
            _show_error(self.app, error)
            return
        self.trim_var.set(self.app.wizard.settings.trim)
        self.case_var.set(self.app.wizard.settings.case_sensitive)
        self.tokens_var.set(empty_tokens_to_text(self.app.wizard.settings.empty_tokens))
        self._rebuild_rules()

    def _save(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.app,
            defaultextension=".json",
            filetypes=[("Rules JSON", "*.json")],
        )
        if not path:
            return
        error = self.presenter.save(path)
        if error is not None:
            _show_error(self.app, error)

    def on_next(self) -> bool:
        ruleset, error = self.presenter.validate()
        if ruleset is None:
            _show_error(self.app, error or self.tr("rules.need_one"))
            return False
        self.app.ruleset = ruleset
        return True


# ------------------------------------------------------------------ step 4: run


class RunStep(StepFrame):
    help_key = "step.run.help"

    def __init__(self, app: App):
        super().__init__(app)
        self.controller = app.controller
        self.grid_rowconfigure(4, weight=1)

        self.progress = ctk.CTkProgressBar(self, mode="determinate")
        self.progress.set(0)
        self.progress.grid(row=1, column=0, sticky="ew", **_PAD)
        self._indeterminate = False

        self.status = ctk.CTkLabel(self, text="", anchor="w")
        self.status.grid(row=2, column=0, sticky="w", **_PAD)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="w", **_PAD)
        self.start_button = ctk.CTkButton(buttons, text=self.tr("run.start"), command=self._start)
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.cancel_button = ctk.CTkButton(
            buttons, text=self.tr("run.cancel"), command=self._cancel, state="disabled"
        )
        self.cancel_button.grid(row=0, column=1)

        counters = ctk.CTkScrollableFrame(self, fg_color="transparent")
        counters.grid(row=4, column=0, sticky="nsew", **_PAD)
        counters.grid_columnconfigure(1, weight=1)
        self.counter_labels: dict[str, ctk.CTkLabel] = {}
        for i, rule in enumerate(app.ruleset.rules):
            ctk.CTkLabel(counters, text=rule.label, anchor="w").grid(
                row=i, column=0, sticky="w", padx=(0, 16), pady=2
            )
            counter = ctk.CTkLabel(counters, text=self.tr("run.matches", count=0), anchor="w")
            counter.grid(row=i, column=1, sticky="w", pady=2)
            self.counter_labels[rule.id] = counter

        self._poll_job: str | None = None
        if not self.controller.running and self.controller.results_valid_for(**self._scan_config()):
            self._show_summary_state()

    def _scan_config(self) -> dict:
        wizard = self.app.wizard
        return {
            "path": wizard.file,
            "ruleset": self.app.ruleset,
            "csv_options": wizard.csv_options,
            "sheet": wizard.sheet,
            "has_header": wizard.has_header,
        }

    def on_show(self) -> None:
        # The frame may be rebuilt while a scan is running (e.g. a language
        # switch): reattach to the worker instead of orphaning it.
        if self.controller.running:
            self.start_button.configure(state="disabled")
            self.cancel_button.configure(state="normal")
            self.app.set_nav_enabled(False)
            self.status.configure(text=self.tr("run.running"))
            self._poll()

    def _start(self) -> None:
        app = self.app
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        app.set_nav_enabled(False)
        self.status.configure(text=self.tr("run.running"))
        self.progress.set(0)
        for label in self.counter_labels.values():
            label.configure(text=self.tr("run.matches", count=0))
        try:
            self.controller.start(**self._scan_config())
        except RuntimeError as exc:
            self.status.configure(text=self.tr("run.error", error=str(exc)))
            return
        self._poll()

    def _cancel(self) -> None:
        self.controller.cancel()
        self.cancel_button.configure(state="disabled")

    def _poll(self) -> None:
        for message in self.controller.drain():
            if message.kind == "progress":
                self._on_progress(message.payload)
            elif message.kind == "done":
                self._on_done()
                return
            else:
                self._on_error(str(message.payload))
                return
        self._poll_job = self.after(100, self._poll)

    def _on_progress(self, progress) -> None:
        fraction = progress.fraction
        if fraction is None:
            if not self._indeterminate:
                self._indeterminate = True
                self.progress.configure(mode="indeterminate")
                self.progress.start()
        else:
            if self._indeterminate:
                self._indeterminate = False
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.set(fraction)
        self.status.configure(text=self.tr("run.rows", rows=f"{progress.rows_processed:,}"))
        for rule_id, count in progress.counts.items():
            label = self.counter_labels.get(rule_id)
            if label is not None:
                label.configure(text=self.tr("run.matches", count=f"{count:,}"))

    def _stop_animation(self) -> None:
        if self._indeterminate:
            self._indeterminate = False
            self.progress.stop()
            self.progress.configure(mode="determinate")

    def _on_done(self) -> None:
        self._stop_animation()
        self._show_summary_state()
        self.app.set_nav_enabled(True)

    def _show_summary_state(self) -> None:
        summary = self.controller.summary
        if summary is None:
            return
        self.progress.set(1.0)
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.status.configure(
            text=self.tr("run.cancelled") if summary.cancelled else self.tr("run.done")
        )
        for rule_id, count in summary.counts.items():
            label = self.counter_labels.get(rule_id)
            if label is not None:
                label.configure(text=self.tr("run.matches", count=f"{count:,}"))

    def _on_error(self, error: str) -> None:
        self._stop_animation()
        self.progress.set(0)
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.status.configure(text=self.tr("run.error", error=error))
        self.app.set_nav_enabled(True)
        _show_error(self.app, self.tr("run.error", error=error))

    def on_next(self) -> bool:
        # Results must exist AND belong to the current file/rules/options —
        # never show a report produced by an earlier configuration.
        return self.controller.results_valid_for(**self._scan_config())

    def destroy(self) -> None:
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
            self._poll_job = None
        super().destroy()


# ------------------------------------------------------------------ step 5: report


class ReportStep(StepFrame):
    help_key = "step.report.help"

    _SAMPLE_CHOICES = ("3", "5", "10", "20")

    def __init__(self, app: App):
        super().__init__(app)
        controller = app.controller
        assert controller.store is not None and controller.summary is not None
        self.presenter = ReportPresenter(
            store=controller.store,
            ruleset=app.ruleset,
            summary=controller.summary,
            file=app.wizard.file,
            sheet=controller.sheet_name,
            header=controller.header,
            width=controller.width,
            csv_options=app.wizard.csv_options,
            tr=app.tr,
            sample_size=3,
        )
        self.grid_rowconfigure(3, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=1, column=0, sticky="ew", **_PAD)
        top.grid_columnconfigure(0, weight=1)
        summary_text = self.presenter.summary_text()
        if self.presenter.partial:
            summary_text += "\n" + self.tr("report.partial")
        ctk.CTkLabel(top, text=summary_text, anchor="w", justify="left").grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkLabel(top, text=self.tr("report.sample_size")).grid(row=0, column=1, padx=(0, 6))
        self.sample_var = ctk.StringVar(value="3")
        ctk.CTkOptionMenu(
            top,
            values=list(self._SAMPLE_CHOICES),
            variable=self.sample_var,
            width=80,
            command=self._change_sample_size,
        ).grid(row=0, column=2)

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="w", **_PAD)
        ctk.CTkButton(actions, text=self.tr("report.export.all"), command=self._export_all).grid(
            row=0, column=0, padx=(0, 8)
        )
        ctk.CTkButton(
            actions, text=self.tr("report.export.report"), command=self._save_report
        ).grid(row=0, column=1)

        self.sections = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.sections.grid(row=3, column=0, sticky="nsew", **_PAD)
        self.sections.grid_columnconfigure(0, weight=1)
        self._tables: dict[str, Table] = {}
        self._positions: dict[str, ctk.CTkLabel] = {}
        self._nav_buttons: dict[str, tuple[ctk.CTkButton, ctk.CTkButton]] = {}
        for i, rule_id in enumerate(self.presenter.rule_ids()):
            self._build_section(i, rule_id)
        self._refresh_all()

    def _build_section(self, index: int, rule_id: str) -> None:
        tr = self.tr
        presenter = self.presenter
        card = ctk.CTkFrame(self.sections)
        card.grid(row=index, column=0, sticky="ew", pady=6)
        card.grid_columnconfigure(0, weight=1)

        title = f"{presenter.rule_label(rule_id)} - " + tr(
            "report.matches", count=f"{presenter.match_count(rule_id):,}"
        )
        ctk.CTkLabel(card, text=title, anchor="w", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", **_PAD
        )
        ctk.CTkButton(
            card,
            text=tr("report.export.rule"),
            width=190,
            command=lambda r=rule_id: self._export_rule(r),
        ).grid(row=0, column=1, sticky="e", **_PAD)

        table = Table(card, height=4)
        table.grid(row=1, column=0, columnspan=2, sticky="ew", **_PAD)
        self._tables[rule_id] = table

        nav = ctk.CTkFrame(card, fg_color="transparent")
        nav.grid(row=2, column=0, columnspan=2, sticky="w", **_PAD)
        prev_button = ctk.CTkButton(
            nav, text=tr("report.prev"), width=110, command=lambda r=rule_id: self._prev(r)
        )
        prev_button.grid(row=0, column=0, padx=(0, 8))
        next_button = ctk.CTkButton(
            nav, text=tr("report.next"), width=110, command=lambda r=rule_id: self._next(r)
        )
        next_button.grid(row=0, column=1, padx=(0, 12))
        position = ctk.CTkLabel(nav, text="")
        position.grid(row=0, column=2)
        self._positions[rule_id] = position
        self._nav_buttons[rule_id] = (prev_button, next_button)

    def _refresh_section(self, rule_id: str) -> None:
        presenter = self.presenter
        columns = [self.tr("report.row_number"), *presenter.columns()]
        self._tables[rule_id].set_data(columns, presenter.page_rows(rule_id))
        self._positions[rule_id].configure(text=presenter.position_text(rule_id))
        prev_button, next_button = self._nav_buttons[rule_id]
        prev_button.configure(state="normal" if presenter.can_prev(rule_id) else "disabled")
        next_button.configure(state="normal" if presenter.can_next(rule_id) else "disabled")

    def _refresh_all(self) -> None:
        for rule_id in self.presenter.rule_ids():
            self._refresh_section(rule_id)

    def _change_sample_size(self, value: str) -> None:
        self.presenter.set_sample_size(int(value))
        self._refresh_all()

    def _prev(self, rule_id: str) -> None:
        self.presenter.prev_page(rule_id)
        self._refresh_section(rule_id)

    def _next(self, rule_id: str) -> None:
        self.presenter.next_page(rule_id)
        self._refresh_section(rule_id)

    def _export_rule(self, rule_id: str) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.app,
            defaultextension=".csv",
            initialfile=f"{rule_id}.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if path:
            self.status_message(self.presenter.export_rule(rule_id, path))

    def _export_all(self) -> None:
        directory = filedialog.askdirectory(parent=self.app)
        if directory:
            self.status_message(self.presenter.export_all(directory))

    def _save_report(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.app,
            defaultextension=".json",
            initialfile="report.json",
            filetypes=[("JSON", "*.json")],
        )
        if path:
            self.status_message(self.presenter.save_report_json(path))

    def status_message(self, text: str) -> None:
        messagebox.showinfo(self.tr("app.title"), text, parent=self.app)

    def destroy(self) -> None:
        self.presenter.close()
        super().destroy()
