"""Floating speech bubble for Doloris desktop companion."""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional


class SpeechBubble:
    """A floating comic-style speech bubble attached to the mascot."""

    def __init__(self, master: tk.Tk):
        self.master = master
        self.window: Optional[tk.Toplevel] = None
        self._hide_job = None
        self._on_click_callback: Optional[Callable[[], None]] = None

    def show(
        self,
        text: str,
        anchor_x: int,
        anchor_y: int,
        duration_ms: int = 6000,
        on_click: Optional[Callable[[], None]] = None,
    ):
        """Displays a message bubble anchored above the given coordinates."""
        self._on_click_callback = on_click
        if self._hide_job:
            self.master.after_cancel(self._hide_job)
            self._hide_job = None

        if not self.window or not self.window.winfo_exists():
            self.window = tk.Toplevel(self.master)
            self.window.overrideredirect(True)
            self.window.attributes("-topmost", True)

            # Bubble container frame with styling
            self.frame = tk.Frame(
                self.window,
                bg="#1e1e2e",
                padx=12,
                pady=8,
                highlightbackground="#89b4fa",
                highlightthickness=2,
            )
            self.frame.pack(fill="both", expand=True)

            self.label = tk.Label(
                self.frame,
                text="",
                font=("Segoe UI", 10, "bold"),
                fg="#cdd6f4",
                bg="#1e1e2e",
                wraplength=220,
                justify="left",
                cursor="hand2" if on_click else "arrow",
            )
            self.label.pack()

            # Click events
            self.window.bind("<Button-1>", self._handle_click)
            self.frame.bind("<Button-1>", self._handle_click)
            self.label.bind("<Button-1>", self._handle_click)

        self.label.config(text=text)
        self.label.config(cursor="hand2" if on_click else "arrow")
        self.window.update_idletasks()

        bw = self.window.winfo_width()
        bh = self.window.winfo_height()

        # Position bubble centered horizontally above the anchor point
        bx = max(10, anchor_x - bw // 2)
        by = max(10, anchor_y - bh - 10)
        self.window.geometry(f"+{bx}+{by}")
        self.window.deiconify()

        if duration_ms > 0:
            self._hide_job = self.master.after(duration_ms, self.hide)

    def _handle_click(self, event):
        if self._on_click_callback:
            self._on_click_callback()
        self.hide()

    def hide(self):
        """Hides the speech bubble."""
        if self._hide_job:
            self.master.after_cancel(self._hide_job)
            self._hide_job = None
        if self.window and self.window.winfo_exists():
            self.window.withdraw()
