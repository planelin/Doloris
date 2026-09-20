"""Main entry point for the Doloris Desktop Companion application."""

from __future__ import annotations

import argparse
import sys
import tkinter as tk

from doloris_app.mascot import DesktopMascot


def main():
    parser = argparse.ArgumentParser(description="Doloris Desktop Companion & Supervision Mascot")
    parser.add_argument("--test-mode", action="store_true", help="Launch in test mode without greeting popup")
    parser.add_argument("--skin", type=str, default="", help="Initial pet skin name to load")
    args = parser.parse_args()

    root = tk.Tk()
    app = DesktopMascot(root, test_mode=args.test_mode)

    if args.skin:
        for s in app.available_skins:
            if s.name == args.skin or s.display_name == args.skin:
                app.set_skin(s)
                break

    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.exit_app()


if __name__ == "__main__":
    main()
