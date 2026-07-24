"""Application shell: the five-step wizard window."""

from __future__ import annotations

import customtkinter as ctk

from lsa.core.i18n import Translator, available_locales, system_locale
from lsa.core.rules import RuleSet
from lsa.gui.presenters import WizardState
from lsa.gui.views import FileStep, ImportStep, ReportStep, RulesStep, RunStep, StepFrame
from lsa.gui.widgets import style_treeviews
from lsa.gui.worker import ScanController

_LANGUAGE_NAMES = {
    "en": "English",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "pt": "Português",
}

_STEPS: list[tuple[str, type[StepFrame]]] = [
    ("step.file.title", FileStep),
    ("step.import.title", ImportStep),
    ("step.rules.title", RulesStep),
    ("step.run.title", RunStep),
    ("step.report.title", ReportStep),
]


class App(ctk.CTk):
    """Main window: header (title, step indicator, language), body, Back/Next."""

    def __init__(self, locale: str | None = None):
        super().__init__()
        self.tr = Translator(locale or system_locale())
        self.wizard = WizardState()
        self.controller = ScanController()
        self.ruleset: RuleSet = RuleSet(rules=())
        self.step_index = 0
        self.step_frame: StepFrame | None = None

        self.title(self.tr("app.title"))
        self.geometry("900x640")
        self.minsize(760, 520)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        style_treeviews()

        header = ctk.CTkFrame(self, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        self.title_label = ctk.CTkLabel(
            header, text=self.tr("app.title"), font=ctk.CTkFont(size=17, weight="bold")
        )
        self.title_label.grid(row=0, column=0, padx=16, pady=10, sticky="w")
        self.step_label = ctk.CTkLabel(header, text="")
        self.step_label.grid(row=0, column=1, sticky="w")
        self.language_var = ctk.StringVar(value=_LANGUAGE_NAMES.get(self.tr.locale, "English"))
        ctk.CTkOptionMenu(
            header,
            values=[_LANGUAGE_NAMES[code] for code in available_locales()],
            variable=self.language_var,
            width=130,
            command=self._change_language,
        ).grid(row=0, column=2, padx=16, pady=10, sticky="e")

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        footer = ctk.CTkFrame(self, corner_radius=0)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.back_button = ctk.CTkButton(
            footer, text=self.tr("nav.back"), width=120, command=self.go_back
        )
        self.back_button.grid(row=0, column=1, padx=(0, 8), pady=10)
        self.next_button = ctk.CTkButton(
            footer, text=self.tr("nav.next"), width=120, command=self.go_next
        )
        self.next_button.grid(row=0, column=2, padx=(0, 16), pady=10)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show_step(0)

    # ------------------------------------------------------------- navigation

    def show_step(self, index: int) -> None:
        """Display step ``index``, rebuilding its frame from current state."""
        if self.step_frame is not None:
            self.step_frame.destroy()
        self.step_index = index
        step_cls = _STEPS[index][1]
        self.step_frame = step_cls(self)
        self.step_frame.grid(row=0, column=0, sticky="nsew")
        self._update_chrome()
        self.step_frame.on_show()

    def go_next(self) -> None:
        if self.step_frame is None or not self.step_frame.on_next():
            return
        if self.step_index < len(_STEPS) - 1:
            self.show_step(self.step_index + 1)

    def go_back(self) -> None:
        if self.step_index > 0:
            self.show_step(self.step_index - 1)

    def set_nav_enabled(self, enabled: bool) -> None:
        """Disable Back/Next while a scan is running."""
        state = "normal" if enabled else "disabled"
        self.back_button.configure(state=state)
        self.next_button.configure(state=state)
        self._update_chrome(keep_nav_state=not enabled)

    def _update_chrome(self, keep_nav_state: bool = False) -> None:
        title_key = _STEPS[self.step_index][0]
        self.step_label.configure(
            text=self.tr("step.indicator", current=self.step_index + 1, total=len(_STEPS))
            + f" - {self.tr(title_key)}"
        )
        if not keep_nav_state:
            self.back_button.configure(state="normal" if self.step_index > 0 else "disabled")
            self.next_button.configure(
                state="normal" if self.step_index < len(_STEPS) - 1 else "disabled"
            )

    # ------------------------------------------------------------- language

    def _change_language(self, display_name: str) -> None:
        for code, name in _LANGUAGE_NAMES.items():
            if name == display_name:
                self.tr.set_locale(code)
                break
        self.title(self.tr("app.title"))
        self.title_label.configure(text=self.tr("app.title"))
        self.back_button.configure(text=self.tr("nav.back"))
        self.next_button.configure(text=self.tr("nav.next"))
        # Rebuild the current step so every label uses the new language.
        self.show_step(self.step_index)

    # ------------------------------------------------------------- lifecycle

    def _on_close(self) -> None:
        self.controller.shutdown()
        self.destroy()


def main() -> None:
    """GUI entry point (``lsa-gui``)."""
    # Must run before anything else: the scan subprocess is spawned by
    # re-executing this entry point in frozen (PyInstaller) builds.
    import multiprocessing

    multiprocessing.freeze_support()
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
