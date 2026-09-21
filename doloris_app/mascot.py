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

    def __init__(self, root: tk.Tk, test_mode: bool = False, scale: float = 1.0):
        self.root = root
        self.test_mode = test_mode
        self.scale = max(0.5, min(3.0, scale))
        self.base_width = 192
        self.base_height = 208
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

        # Canvas for rendering the sprite (scaled)
        c_w = int(self.base_width * self.scale)
        c_h = int(self.base_height * self.scale)
        self.canvas = tk.Canvas(
            self.root,
            width=c_w,
            height=c_h,
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
        self.canvas.bind("<Control-MouseWheel>", self._on_ctrl_wheel)

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
        w = max(10, int(self.base_width * self.scale))
        h = max(10, int(self.base_height * self.scale))
        x = max(0, sw - w - 60)
        y = max(0, sh - h - 100)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _on_ctrl_wheel(self, event):
        """Ctrl + MouseWheel to zoom in/out smoothly between 5% and 500%."""
        step = 0.05 if event.delta > 0 else -0.05
        new_scale = round(self.scale + step, 2)
        if 0.05 <= new_scale <= 5.0:
            self.set_scale(new_scale)

    def set_scale(self, new_scale: float):
        """Dynamically changes the mascot scale factor (clamped 5% to 500%), resizing window and frames."""
        self.scale = max(0.05, min(5.0, new_scale))
        w = max(10, int(self.base_width * self.scale))
        h = max(10, int(self.base_height * self.scale))
        self.canvas.config(width=w, height=h)
        self._position_bottom_right()
        self._load_current_state_frames()
        pct_display = int(round(self.scale * 100))
        self.show_bubble(f"已调整桌宠大小为：{pct_display}% 🐾", duration_ms=3000)

    def prompt_custom_scale(self):
        """Prompt user to input a custom scaling percentage between 5% and 500%."""
        from tkinter import simpledialog, messagebox
        curr_pct = int(round(self.scale * 100))
        val_str = simpledialog.askstring(
            "调整桌宠显示大小",
            f"请输入缩放比例 (5% ~ 500%)，当前为 {curr_pct}%：\n（例如输入 80 代表 80%，150 代表 150%）",
            parent=self.root,
            initialvalue=str(curr_pct),
        )
        if val_str is None:
            return
        clean = val_str.strip().rstrip("%").strip()
        try:
            pct = float(clean)
            if 5.0 <= pct <= 500.0:
                self.set_scale(pct / 100.0)
            else:
                messagebox.showwarning(
                    "数值超出范围",
                    "缩放比例数值必须在 5% 至 500% 之间！",
                    parent=self.root,
                )
        except ValueError:
            messagebox.showerror(
                "输入无效",
                "请输入有效的数字（如 50、100、125）！",
                parent=self.root,
            )

    def _create_context_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0, font=("Segoe UI", 9))

        # Mode submenu (populated dynamically in _rebuild_start_menu)
        self.start_menu = tk.Menu(self.menu, tearoff=0, font=("Segoe UI", 9))
        self._rebuild_start_menu()

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

        # Scale command (direct prompt dialog 5% ~ 500%)
        self.menu.add_command(
            label=f"🔍 调整显示大小 (输入 5%~500%)...",
            command=self.prompt_custom_scale,
        )
        self.menu.add_separator()

        self.menu.add_command(label="📄 查看最新终态报告", command=self.open_latest_report)
        self.menu.add_command(label="❌ 退出桌宠", command=self.exit_app)

    def _rebuild_start_menu(self):
        self.start_menu.delete(0, "end")
        try:
            from afk_supervisor.sessions.discovery import list_recent_codex_sessions
            sessions = list_recent_codex_sessions(8)
        except Exception:
            sessions = []

        modes = [
            ("fork", "🔱 Fork 无头续跑 (推荐，不杀App)"),
            ("resume", "⚡ 经典安全接管 (Kill & Resume)"),
            ("gui", "🖥️ 双有头原生 GUI 注入"),
        ]

        for mode_key, mode_label in modes:
            sub = tk.Menu(self.start_menu, tearoff=0, font=("Segoe UI", 9))
            sub.add_command(
                label="⭐ 最新活跃会话 (Last)",
                command=lambda m=mode_key: self.start_mode(m, "last")
            )
            if sessions:
                sub.add_separator()
                for i, (sid, _, _, title, age) in enumerate(sessions, 1):
                    clean_title = (title or "无标题任务").replace("\n", " ").strip()
                    if len(clean_title) > 26:
                        clean_title = clean_title[:26] + "..."
                    label_text = f"[{i}] {age} [{sid[:8]}] {clean_title}"
                    sub.add_command(
                        label=label_text,
                        command=lambda m=mode_key, idx=str(i): self.start_mode(m, idx)
                    )
            sub.add_separator()
            sub.add_command(
                label="✏️ 手动输入 Session ID / URL...",
                command=lambda m=mode_key: self._prompt_custom_session(m)
            )
            self.start_menu.add_cascade(label=mode_label, menu=sub)

        self.start_menu.add_separator()
        self.start_menu.add_command(label="📋 完整会话选择器...", command=self._show_session_picker_dialog)

    def _prompt_custom_session(self, mode: str):
        from tkinter import simpledialog
        sid = simpledialog.askstring("自定义会话接管", "请输入 Codex 会话 ID 或 URL (如 codex://threads/...):", parent=self.root)
        if sid and sid.strip():
            self.start_mode(mode, sid.strip())

    def _show_session_picker_dialog(self):
        try:
            from afk_supervisor.sessions.discovery import list_recent_codex_sessions
            sessions = list_recent_codex_sessions(12)
        except Exception:
            sessions = []

        win = tk.Toplevel(self.root)
        win.title("Doloris — Codex 会话选择器")
        win.attributes("-topmost", True)
        win.geometry("560x420")
        win.resizable(False, False)

        frame = tk.Frame(win, padx=12, pady=10)
        frame.pack(fill="both", expand=True)

        lbl = tk.Label(frame, text="请选择要接管的 Codex 历史会话：", font=("Segoe UI", 10, "bold"))
        lbl.pack(anchor="w", pady=(0, 6))

        listbox_frame = tk.Frame(frame)
        listbox_frame.pack(fill="both", expand=True)

        scrollbar = tk.Scrollbar(listbox_frame)
        scrollbar.pack(side="right", fill="y")

        lb = tk.Listbox(listbox_frame, font=("Segoe UI", 9), yscrollcommand=scrollbar.set, selectmode="single")
        lb.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=lb.yview)

        lb.insert("end", "⭐ [最新活跃会话] 自动选择最近修改的会话 (last)")
        for i, (sid, _, scwd, title, age) in enumerate(sessions, 1):
            clean_title = (title or "无标题任务").replace("\n", " ").strip()
            lb.insert("end", f"[{i}]  {age}  [{sid[:8]}]  {clean_title}")

        lb.selection_set(0)

        # Custom SID entry
        custom_frame = tk.Frame(frame)
        custom_frame.pack(fill="x", pady=6)
        tk.Label(custom_frame, text="或输入自定义 ID / URL:", font=("Segoe UI", 9)).pack(side="left")
        custom_entry = tk.Entry(custom_frame, font=("Segoe UI", 9))
        custom_entry.pack(side="left", fill="x", expand=True, padx=6)

        # Mode selection radio buttons
        mode_var = tk.StringVar(value="fork")
        mode_frame = tk.Frame(frame)
        mode_frame.pack(fill="x", pady=6)
        tk.Label(mode_frame, text="托管模式:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        tk.Radiobutton(mode_frame, text="Fork 无损续跑 (推荐)", variable=mode_var, value="fork").pack(side="left", padx=4)
        tk.Radiobutton(mode_frame, text="Resume 原地接管", variable=mode_var, value="resume").pack(side="left", padx=4)
        tk.Radiobutton(mode_frame, text="GUI 注入", variable=mode_var, value="gui").pack(side="left", padx=4)

        # Buttons
        btn_frame = tk.Frame(frame)
        btn_frame.pack(fill="x", pady=(8, 0))

        def on_confirm():
            raw_input = custom_entry.get().strip()
            chosen_mode = mode_var.get()
            if raw_input:
                target = raw_input
            else:
                sel = lb.curselection()
                if sel and sel[0] == 0:
                    target = "last"
                elif sel and sel[0] > 0:
                    target = str(sel[0])  # 1-indexed for sessions
                else:
                    target = "last"
            win.destroy()
            self.start_mode(chosen_mode, target)

        confirm_btn = tk.Button(btn_frame, text="🚀 开始托管", font=("Segoe UI", 9, "bold"), bg="#4CAF50", fg="white", padx=16, pady=4, command=on_confirm)
        confirm_btn.pack(side="right", padx=4)
        cancel_btn = tk.Button(btn_frame, text="取消", font=("Segoe UI", 9), padx=12, pady=4, command=win.destroy)
        cancel_btn.pack(side="right")

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

        target_w = max(10, int(self.base_width * self.scale))
        target_h = max(10, int(self.base_height * self.scale))

        for p_img in pil_frames:
            img = p_img
            if img.size != (target_w, target_h):
                img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

            # Hard-threshold alpha to absolutely eliminate dirty black halos against Windows CHROMA_KEY
            r, g, b, a = img.split()
            a = a.point(lambda p: 255 if p > 80 else 0)
            clean_img = Image.merge("RGBA", (r, g, b, a))

            # Composite transparent RGBA image over CHROMA_KEY background
            bg = Image.new("RGBA", clean_img.size, CHROMA_KEY)
            comp = Image.alpha_composite(bg, clean_img)
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
        rx = self.root.winfo_x() + int(self.base_width * self.scale // 2)
        ry = self.root.winfo_y()
        self.bubble.show(text, rx, ry, duration_ms=duration_ms, on_click=on_click)

    def on_supervision_event(self, state: str, message: str, report_path: Optional[str]):
        """Callback invoked by SupervisionController on background thread."""
        def update():
            self.set_state(state)
            on_click = (lambda: self._open_file(report_path)) if report_path else None
            self.show_bubble(message, duration_ms=8000 if report_path else 5000, on_click=on_click)

        self.root.after(0, update)

    def start_mode(self, mode: str = "fork", session_target: str = "last"):
        self.controller.start_supervision(mode, session_target)

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
            no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(["notepad.exe", path], creationflags=no_win)

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
        self._rebuild_start_menu()
        self.menu.tk_popup(event.x_root, event.y_root)
