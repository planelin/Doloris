"""Desktop Mascot floating window for Doloris."""

from __future__ import annotations

import os
import subprocess
import tkinter as tk
from typing import List, Optional

from PIL import Image, ImageTk

from doloris_app.bubble import SpeechBubble
from doloris_app.controller import SupervisionController
from doloris_app.pet_loader import PetSkin, create_default_pet_skin, discover_available_pets

CHROMA_KEY = "#000001"  # Pure near-black chroma key for Windows transparency


class DesktopMascot:
    """Floating transparent desktop mascot window."""

    def __init__(self, root: tk.Tk, test_mode: bool = False):
        self.root = root
        self.test_mode = test_mode
        self.root.title("Doloris Desktop Companion")

        # Window styling: frameless, topmost, transparent background
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", CHROMA_KEY)
        self.root.config(bg=CHROMA_KEY)

        # Mascot state
        self.available_skins = discover_available_pets()
        self.current_skin = self.available_skins[0]
        self.current_state = "idle"
        self.current_frames_tk: List[ImageTk.PhotoImage] = []
        self.frame_index = 0
        self.animation_interval_ms = 150

        # Dragging variables
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._is_dragging = False

        # Canvas for rendering the sprite
        self.canvas = tk.Canvas(
            self.root,
            width=192,
            height=208,
            bg=CHROMA_KEY,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack()

        # Speech bubble
        self.bubble = SpeechBubble(self.root)

        # Supervision controller
        self.controller = SupervisionController(self.on_supervision_event)

        # Context menu
        self._create_context_menu()

        # Bindings
        self.canvas.bind("<Button-1>", self._on_left_down)
        self.canvas.bind("<B1-Motion>", self._on_left_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_left_up)
        self.canvas.bind("<Button-3>", self._on_right_click)

        # Position at bottom-right of primary screen
        self._position_bottom_right()

        # Load initial frames and start animation loop
        self._load_current_state_frames()
        self._animate()

        # Initial greeting bubble
        if not self.test_mode:
            self.root.after(800, lambda: self.show_bubble("主人好！右键点击我可一键开启 Codex 托管哦~ (ง •̀_•́)ง"))

    def _position_bottom_right(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = 192
        h = 208
        x = sw - w - 60
        y = sh - h - 100
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _create_context_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0, font=("Segoe UI", 9))

        # Mode submenu
        self.start_menu = tk.Menu(self.menu, tearoff=0, font=("Segoe UI", 9))
        self.start_menu.add_command(label="🔱 Fork 无头续跑 (推荐，不杀App)", command=lambda: self.start_mode("fork"))
        self.start_menu.add_command(label="⚡ 经典安全接管 (Kill & Resume)", command=lambda: self.start_mode("resume"))
        self.start_menu.add_command(label="🖥️ 双有头原生 GUI 注入", command=lambda: self.start_mode("gui"))

        self.menu.add_cascade(label="🚀 开始托管", menu=self.start_menu)
        self.menu.add_command(label="⏹️ 停止托管", command=self.stop_mode)
        self.menu.add_separator()

        # Skin submenu
        self.skin_menu = tk.Menu(self.menu, tearoff=0, font=("Segoe UI", 9))
        for skin in self.available_skins:
            self.skin_menu.add_command(
                label=f"🐾 {skin.display_name}",
                command=lambda s=skin: self.set_skin(s),
            )
        self.menu.add_cascade(label="🎨 更换桌宠皮肤", menu=self.skin_menu)
        self.menu.add_separator()

        self.menu.add_command(label="📄 查看最新终态报告", command=self.open_latest_report)
        self.menu.add_command(label="❌ 退出桌宠", command=self.exit_app)

    def set_skin(self, skin: PetSkin):
        self.current_skin = skin
        self._load_current_state_frames()
        self.show_bubble(f"已切换为桌宠皮肤：{skin.display_name} 🐾")

    def set_state(self, state: str):
        if self.current_state != state:
            self.current_state = state
            self._load_current_state_frames()

    def _load_current_state_frames(self):
        pil_frames = self.current_skin.get_frames(self.current_state)
        self.current_frames_tk = []

        for p_img in pil_frames:
            # Composite transparent RGBA image over CHROMA_KEY background
            bg = Image.new("RGBA", p_img.size, CHROMA_KEY)
            comp = Image.alpha_composite(bg, p_img)
            self.current_frames_tk.append(ImageTk.PhotoImage(comp))

        self.frame_index = 0

    def _animate(self):
        if self.current_frames_tk:
            frame = self.current_frames_tk[self.frame_index % len(self.current_frames_tk)]
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=frame)
            self.frame_index += 1

        self.root.after(self.animation_interval_ms, self._animate)

    def show_bubble(self, text: str, duration_ms: int = 6000, on_click=None):
        rx = self.root.winfo_x() + 96
        ry = self.root.winfo_y()
        self.bubble.show(text, rx, ry, duration_ms=duration_ms, on_click=on_click)

    def on_supervision_event(self, state: str, message: str, report_path: Optional[str]):
        """Callback invoked by SupervisionController on background thread."""
        def update():
            self.set_state(state)
            on_click = (lambda: self._open_file(report_path)) if report_path else None
            self.show_bubble(message, duration_ms=8000 if report_path else 5000, on_click=on_click)

        self.root.after(0, update)

    def start_mode(self, mode: str):
        self.controller.start_supervision(mode)

    def stop_mode(self):
        self.controller.stop_supervision()

    def open_latest_report(self):
        report = self.controller.latest_report_path or self.controller._find_latest_report()
        if report and os.path.exists(report):
            self._open_file(report)
        else:
            self.show_bubble("暂无历史终态报告 (runs/ 尚未生成)")

    def _open_file(self, path: str):
        try:
            os.startfile(path)
        except Exception:
            subprocess.Popen(["notepad.exe", path])

    def exit_app(self):
        self.controller.stop_supervision()
        self.bubble.hide()
        self.root.destroy()

    # --- Mouse Event Handlers ---
    def _on_left_down(self, event):
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._is_dragging = False

    def _on_left_drag(self, event):
        dx = event.x - self._drag_start_x
        dy = event.y - self._drag_start_y
        if abs(dx) > 3 or abs(dy) > 3:
            self._is_dragging = True
            new_x = self.root.winfo_x() + dx
            new_y = self.root.winfo_y() + dy
            self.root.geometry(f"+{new_x}+{new_y}")
            # Update bubble position if visible
            if self.bubble.window and self.bubble.window.winfo_viewable():
                self.bubble.show(
                    self.bubble.label.cget("text"),
                    new_x + 96,
                    new_y,
                    duration_ms=0,
                )

    def _on_left_up(self, event):
        if not self._is_dragging:
            # Simple click: trigger playful response or status
            if self.controller.is_running():
                self.show_bubble("正在全力监工中！点击右键可停止或查看状态~ 🐾")
            else:
                self.show_bubble("主人去忙吧！右键点我可以开启 Codex 托管哦~ (๑•̀ㅂ•́)و✧")

    def _on_right_click(self, event):
        self.menu.tk_popup(event.x_root, event.y_root)
