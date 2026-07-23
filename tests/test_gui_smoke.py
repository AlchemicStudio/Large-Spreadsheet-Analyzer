"""End-to-end GUI smoke test (needs a display; run with: pytest -m gui).

On headless Linux: xvfb-run -a uv run pytest -m gui
"""

import time

import pytest

from lsa.core.csv_stream import CsvOptions
from lsa.gui.presenters import ConditionDraft, RuleDraft

pytestmark = pytest.mark.gui


@pytest.fixture
def app():
    ctk = pytest.importorskip("customtkinter")
    from lsa.gui.app import App

    ctk.set_appearance_mode("light")
    try:
        application = App(locale="en")
    except Exception as exc:  # no display
        pytest.skip(f"cannot open a Tk window: {exc}")
    yield application
    application._on_close()


def _pump(app, seconds: float = 0.2) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.update()
        time.sleep(0.01)


def test_wizard_end_to_end(app, make_csv) -> None:
    from lsa.gui.views import ImportStep, ReportStep, RulesStep, RunStep

    path = make_csv("name,email\nAna,\nBo,b@c.d\nCy,\n")

    # Step 1: choose file
    app.step_frame.path_var.set(str(path))
    app.go_next()
    assert isinstance(app.step_frame, ImportStep)

    # Step 2: pin deterministic CSV options (the dialog would do this)
    app.wizard.csv_options = CsvOptions(has_header=True)
    app.wizard.has_header = True
    app.go_next()
    assert isinstance(app.step_frame, RulesStep)

    # Step 3: build one rule programmatically
    app.wizard.rule_drafts = [
        RuleDraft(
            id="no-email",
            label="Missing e-mail",
            match="all",
            conditions=[ConditionDraft(type="is_empty", column_by="header", column_value="email")],
        )
    ]
    app.go_next()
    assert isinstance(app.step_frame, RunStep)
    assert [r.id for r in app.ruleset.rules] == ["no-email"]

    # Step 4: run the scan and wait for the worker + after() poll loop
    app.step_frame._start()
    for _ in range(300):
        _pump(app, 0.05)
        if app.controller.summary is not None and app.step_frame.on_next():
            break
    assert app.controller.summary is not None
    assert app.controller.summary.counts == {"no-email": 2}
    _pump(app, 0.3)  # let the poll loop consume the 'done' message

    # Step 5: report
    app.go_next()
    assert isinstance(app.step_frame, ReportStep)
    rows = app.step_frame.presenter.page_rows("no-email")
    assert [r[0] for r in rows] == ["2", "4"]


def test_language_switch_rebuilds_labels(app) -> None:
    app._change_language("Français")
    _pump(app, 0.1)
    assert "Étape" in app.step_label.cget("text")
    assert app.next_button.cget("text") == "Suivant"
    app._change_language("English")


def test_csv_dialog_preview(app, make_csv) -> None:
    from lsa.gui.views import CsvImportDialog

    path = make_csv("a;b\n1;2\n")
    app.step_frame.path_var.set(str(path))
    app.go_next()
    frame = app.step_frame
    dialog = CsvImportDialog(app, frame.presenter, on_applied=lambda: None)
    _pump(app, 0.5)
    assert dialog.table.tree.get_children()
    dialog.separator_var.set(";")
    dialog.header_var.set(True)
    dialog._apply()
    _pump(app, 0.2)
    assert app.wizard.csv_options.separator == ";"
    assert app.wizard.csv_options.has_header is True
