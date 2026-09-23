"""Main entry point for the Doloris Desktop Companion application."""

from __future__ import annotations

import argparse
import tkinter as tk

from doloris_app.mascot import DesktopMascot


def main():
    parser = argparse.ArgumentParser(description="Doloris Desktop Companion & Supervision Mascot")
    parser.add_argument("--test-mode", action="store_true", help="Launch in test mode without greeting popup")
    parser.add_argument("--skin", type=str, default="", help="Initial pet skin name to load")
    parser.add_argument("--scale", type=float, default=1.0, help="Initial display scale factor (e.g. 1.25, 1.5, 2.0)")
    args = parser.parse_args()
    scale = args.scale
    if scale > 5.0:
        scale = scale / 100.0
    scale = max(0.05, min(5.0, scale))

    root = tk.Tk()
    app = DesktopMascot(root, test_mode=args.test_mode, scale=scale)

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
    try:
        main()
    except Exception:
        import traceback
        err_msg = traceback.format_exc()
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("Doloris 启动错误", f"桌宠启动失败：\n{err_msg}")
            r.destroy()
        except Exception:
            pass
        raise
