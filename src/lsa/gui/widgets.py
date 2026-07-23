"""Small Tk/CTk helpers: dark-mode-aware Treeview styling and table building."""

from __future__ import annotations

from tkinter import ttk

import customtkinter as ctk

_ACCENT = "#1f6aa5"


def style_treeviews() -> None:
    """Style ttk.Treeview to match the current CustomTkinter appearance mode.

    CustomTkinter has no table widget; plain ttk.Treeview ignores dark mode
    unless restyled with the ``clam`` theme.  Call again whenever the
    appearance mode changes.
    """
    dark = ctk.get_appearance_mode() == "Dark"
    bg = "#2b2b2b" if dark else "#f4f4f4"
    fg = "#dce4ee" if dark else "#1a1a1a"
    head_bg = "#212121" if dark else "#dbdbdb"
    style = ttk.Style()
    style.theme_use("clam")
    style.configure(
        "Treeview",
        background=bg,
        foreground=fg,
        fieldbackground=bg,
        borderwidth=0,
        rowheight=24,
    )
    style.configure(
        "Treeview.Heading", background=head_bg, foreground=fg, borderwidth=0, relief="flat"
    )
    style.map("Treeview", background=[("selected", _ACCENT)])
    style.map("Treeview.Heading", background=[("active", head_bg)])


class Table(ctk.CTkFrame):
    """A ttk.Treeview with scrollbars, filled from plain lists of strings."""

    def __init__(self, master, *, height: int = 8):
        super().__init__(master, fg_color="transparent")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(self, show="headings", height=height, selectmode="none")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

    def set_data(self, columns: list[str], rows: list[list[str]]) -> None:
        """Replace all columns and rows."""
        tree = self.tree
        tree.delete(*tree.get_children())
        ids = [f"c{i}" for i in range(len(columns))]
        tree.configure(columns=ids)
        for column_id, title in zip(ids, columns, strict=True):
            tree.heading(column_id, text=title)
            tree.column(column_id, width=110, minwidth=60, stretch=False)
        for row in rows:
            values = list(row[: len(ids)]) + [""] * max(0, len(ids) - len(row))
            tree.insert("", "end", values=values)
