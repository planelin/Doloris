"""Desktop Mascot floating window for Doloris."""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from typing import List, Optional

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    Image = None  # type: ignore
    ImageTk = None  # type: ignore
    HAS_PIL = False

from doloris_app.bubble import SpeechBubble
from doloris_app.controller import SupervisionController
from doloris_app.pet_loader import PetSkin, discover_available_pets

CHROMA_KEY = "#000001"  # Pure near-black chroma key for Windows transparency


def resolve_lazy_goal(rollout_path: Optional[Path], title: str = "", codex_session_id: str = "",
                      cancel_event: Optional[threading.Event] = None) -> str:
    """懒人模式只展示 AGY 已完成的目标；失败时保持输入框为空。"""
    from afk_supervisor.goal_engine import GoalExtractionError, extract_clean_goal

    rollout = Path(rollout_path) if rollout_path else None
    goal = extract_clean_goal(rollout, title=title, codex_session_id=codex_session_id,
                              require_agy=True, cancel_event=cancel_event)
    if not goal:
        raise GoalExtractionError("AGY 未返回有效目标")
    return goal


class DesktopMascot:
    """Floating transparent desktop mascot window."""

    def __init__(self, root: tk.Tk, test_mode: bool = False, scale: float = 1.0):
        self.root = root
        self.test_mode = test_mode
        self.scale = max(0.5, min(3.0, scale))
        self.base_width = 192
        self.base_height = 208
        self.root.title("Doloris Desktop Companion")
        if not HAS_PIL:
            raise RuntimeError("Pillow is required for the desktop mascot. Install it with: pip install Pillow")

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

        self.menu.add_cascade(label="🚀 托管模式", menu=self.start_menu)
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
            label="🔍 调整显示大小...",
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
            ("goal", "🎯 Goal 目标模式"),
            ("fork", "🔱 Fork 无头并发"),
            ("resume", "⚡ Kill 快速接管"),
            ("gui", "🖥 Gui  有头注入"),
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
                        command=lambda m=mode_key, s=sid: self.start_mode(m, s)
                    )
            sub.add_separator()
            sub.add_command(
                label="✏️ 手动输入 Session ID / URL...",
                command=lambda m=mode_key: self._prompt_custom_session(m)
            )
            self.start_menu.add_cascade(label=mode_label, menu=sub)

        self.start_menu.add_separator()
        self.start_menu.add_command(label="📋 完整会话选择器...", command=self._show_session_picker_dialog)

    def prompt_goal_mode(self, session_target: str = "last"):
        """Pops up Goal Mode definition dialog or automatically resumes existing goal."""
        from afk_supervisor.sessions.discovery import clean_session_id
        clean_target = clean_session_id(session_target) if session_target else "last"
        rollout_path = None
        actual_sid = None
        target_title = ""
        try:
            if clean_target == "last":
                from afk_supervisor.sessions.discovery import find_last_codex_session, read_session_title
                last_got = find_last_codex_session()
                if last_got:
                    actual_sid, rollout_path, _ = last_got
                    if rollout_path:
                        target_title = read_session_title(Path(rollout_path))
            elif clean_target.isdigit():
                idx = int(clean_target)
                from afk_supervisor.sessions.discovery import list_recent_codex_sessions
                recent = list_recent_codex_sessions(max(idx, 8))
                if 1 <= idx <= len(recent):
                    actual_sid, rollout_path, _, target_title, _ = recent[idx - 1]
            else:
                from afk_supervisor.sessions.discovery import find_codex_session_by_id, read_session_title
                got = find_codex_session_by_id(clean_target)
                if got:
                    actual_sid, rollout_path, _ = got
                    if rollout_path:
                        target_title = read_session_title(Path(rollout_path))
        except Exception:
            pass

        target_for_mode = actual_sid or clean_target or session_target

        # 核心逻辑 1：先严格检测是否已设立真实目标。若已有真实活跃目标且处于阶段2/3，直接进入自主推进，绝不重复弹窗！
        if rollout_path and Path(rollout_path).exists():
            from afk_supervisor.goal_engine import get_existing_thread_goal, check_plan_status, DUMMY_GOAL_MARKERS
            existing_goal = get_existing_thread_goal(Path(rollout_path))
            phase = check_plan_status(Path(rollout_path))
            if existing_goal and existing_goal not in DUMMY_GOAL_MARKERS and phase in ("planning", "executing"):
                goal_disp = existing_goal
                phase_name = "长程执行中" if phase == "executing" else "计划制定中"
                disp = goal_disp[:26] + "..." if len(goal_disp) > 26 else goal_disp
                self.show_bubble(f"🎯 会话已设立目标 [{phase_name}]：\n「{disp}」\n已跳过设定，直接开启自主推进！🐾", duration_ms=6000)
                self.start_mode("goal", target_for_mode, goal_target="__ALREADY_SET__")
                return

        # 核心逻辑 2：若确认需要设立目标，弹窗留给用户的时间不设限制！
        win = tk.Toplevel(self.root)
        win.title("Doloris — 开启 Goal 目标模式")
        win.attributes("-topmost", True)
        win.geometry("520x280")
        win.resizable(False, False)

        # 居中显示
        try:
            sw = int(win.winfo_screenwidth())
            sh = int(win.winfo_screenheight())
            wx = max(0, (sw - 520) // 2)
            wy = max(0, (sh - 280) // 2)
            win.geometry(f"520x280+{wx}+{wy}")
        except Exception:
            win.geometry("520x280")

        frame = tk.Frame(win, padx=16, pady=14)
        frame.pack(fill="both", expand=True)

        target_hint = f"会话 [{session_target[:8]}]" if session_target != "last" else "最新活跃会话"
        lbl_title = tk.Label(frame, text=f"🎯 为当前任务开启 Goal 自主模式 ({target_hint})", font=("Segoe UI", 11, "bold"), fg="#1e293b")
        lbl_title.pack(anchor="w", pady=(0, 4))

        lbl_desc = tk.Label(
            frame,
            text="请输入离席无人值守要达成的具体目标，或点击【懒人模式 (AGY提炼)】由 AGY 根据上下文提炼目标：\n（手动输入不设时间限制；AGY 提炼完成后将开启 30s 确认倒计时）",
            font=("Segoe UI", 9),
            justify="left",
            fg="#475569",
            wraplength=480,
        )
        lbl_desc.pack(anchor="w", pady=(0, 8))

        entry = tk.Entry(frame, font=("Segoe UI", 10))
        entry.pack(fill="x", pady=(0, 12))
        entry.focus_set()

        btn_frame = tk.Frame(frame)
        btn_frame.pack(fill="x", side="bottom")

        import queue
        result_q: queue.Queue = queue.Queue()
        user_manually_edited = [False]

        remaining = [30]
        timer_id = [None]
        timer_paused = [False]
        is_countdown_active = [False]
        is_lazy_working = [False]
        lazy_cancel = threading.Event()

        def on_entry_key(event):
            if event.keysym not in ("Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Return", "Escape"):
                user_manually_edited[0] = True

        entry.bind("<Key>", on_entry_key)

        def cancel_timer():
            if timer_id[0] is not None:
                try:
                    win.after_cancel(timer_id[0])
                except Exception:
                    pass
                timer_id[0] = None
            is_countdown_active[0] = False

        def on_confirm_goal():
            val = entry.get().strip()
            if not val:
                lbl_desc.config(text="请先输入目标；需要 AGY 帮忙时请点击【懒人模式 (AGY提炼)】。", fg="#dc2626")
                entry.focus_set()
                return
            cancel_timer()
            lazy_cancel.set()
            win.destroy()
            self.show_bubble(f"🎯 已确立 Goal 目标：{val[:20]}... 开始冲刺！", duration_ms=5000)
            self.start_mode("goal", target_for_mode, goal_target=val)

        def on_cancel():
            cancel_timer()
            lazy_cancel.set()
            win.destroy()

        def toggle_pause():
            if not is_countdown_active[0]:
                return
            timer_paused[0] = not timer_paused[0]
            if timer_paused[0]:
                pause_btn.config(text="▶️ 继续倒计时", bg="#e2e8f0")
                lbl_desc.config(text="⏸️ 倒计时已暂停，您可以仔细修改润色目标。完成后点击【继续倒计时】或【立即启动】：", fg="#475569")
            else:
                pause_btn.config(text="⏸️ 暂停计时", bg="#f1f5f9")
                lbl_desc.config(text=f"▶️ 倒计时已恢复，剩余 {remaining[0]}s 后自动启动：", fg="#16a34a")

        def tick():
            if not is_countdown_active[0]:
                return
            if not timer_paused[0]:
                remaining[0] -= 1
                if remaining[0] <= 0:
                    cancel_timer()
                    on_confirm_goal()
                    return
                else:
                    confirm_btn.config(text=f"🚀 立即启动 ({remaining[0]}s)")
            timer_id[0] = win.after(1000, tick)

        def start_30s_countdown():
            cancel_timer()
            remaining[0] = 30
            timer_paused[0] = False
            is_countdown_active[0] = True
            confirm_btn.config(text="🚀 立即启动 (30s)")
            pause_btn.pack(side="right", padx=4)
            timer_id[0] = win.after(1000, tick)

        def poll_lazy_result():
            if not win.winfo_exists():
                return
            try:
                status, agy_res = result_q.get_nowait()
            except queue.Empty:
                if is_lazy_working[0]:
                    win.after(100, poll_lazy_result)
                return

            is_lazy_working[0] = False
            if status == "ok" and agy_res:
                if not user_manually_edited[0] or not entry.get().strip():
                    entry.delete(0, "end")
                    entry.insert(0, agy_res)
                lazy_btn.config(text="✅ AGY 提炼完成", state="normal")
                lbl_desc.config(text="✅ AGY 已为您提炼交付目标！您可以直接润色修改，30s 后将自动启动：", fg="#16a34a")
                start_30s_countdown()
            else:
                lazy_btn.config(text="🤖 懒人模式 (AGY提炼)", state="normal")
                lbl_desc.config(text="⚠️ AGY 尚未返回目标，请重试或手动输入；输入框未被改动。", fg="#dc2626")

        def on_lazy_goal():
            if is_lazy_working[0]:
                return
            cancel_timer()
            is_lazy_working[0] = True
            lazy_btn.config(text="⏳ 正在呼叫 AGY 提炼...", state="disabled")
            lbl_desc.config(text="🤖 Antigravity AGY 正在阅读上下文提炼干净目标，请稍候...", fg="#2563eb")

            def worker():
                try:
                    goal = resolve_lazy_goal(
                        rollout_path,
                        title=target_title,
                        codex_session_id=target_for_mode,
                        cancel_event=lazy_cancel,
                    )
                    result_q.put(("ok", goal))
                except Exception as exc:
                    result_q.put(("error", str(exc)))

            threading.Thread(target=worker, daemon=True).start()
            win.after(100, poll_lazy_result)

        entry.bind("<Return>", lambda e: on_confirm_goal())

        confirm_btn = tk.Button(
            btn_frame,
            text="🚀 启动 Goal 模式",
            font=("Segoe UI", 9, "bold"),
            bg="#2563eb",
            fg="white",
            padx=14,
            pady=4,
            command=on_confirm_goal,
        )
        confirm_btn.pack(side="right", padx=4)

        pause_btn = tk.Button(
            btn_frame,
            text="⏸️ 暂停计时",
            font=("Segoe UI", 9),
            bg="#f1f5f9",
            fg="#0f172a",
            padx=10,
            pady=4,
            command=toggle_pause,
        )

        lazy_btn = tk.Button(
            btn_frame,
            text="🤖 懒人模式 (AGY提炼)",
            font=("Segoe UI", 9),
            bg="#f1f5f9",
            fg="#0f172a",
            padx=10,
            pady=4,
            command=on_lazy_goal,
        )
        lazy_btn.pack(side="right", padx=4)

        cancel_btn = tk.Button(btn_frame, text="取消", font=("Segoe UI", 9), padx=10, pady=4, command=on_cancel)
        cancel_btn.pack(side="right")

        win.protocol("WM_DELETE_WINDOW", on_cancel)

        # 只有用户主动点击“懒人模式”才会启动 AGY。

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
        mode_var = tk.StringVar(value="goal")
        mode_frame = tk.Frame(frame)
        mode_frame.pack(fill="x", pady=6)
        tk.Label(mode_frame, text="托管模式:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        tk.Radiobutton(mode_frame, text="🎯 Goal 目标模式", variable=mode_var, value="goal").pack(side="left", padx=4)
        tk.Radiobutton(mode_frame, text="🔱 Fork 无头并发", variable=mode_var, value="fork").pack(side="left", padx=4)
        tk.Radiobutton(mode_frame, text="⚡ Kill 快速接管", variable=mode_var, value="resume").pack(side="left", padx=4)
        tk.Radiobutton(mode_frame, text="🖥 Gui  有头注入", variable=mode_var, value="gui").pack(side="left", padx=4)

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
                elif sel and sel[0] > 0 and (sel[0] - 1) < len(sessions):
                    target = sessions[sel[0] - 1][0]  # 真实会话 UUID
                else:
                    target = "last"
            win.destroy()
            if chosen_mode == "goal":
                self.prompt_goal_mode(target)
            else:
                self.start_mode(chosen_mode, target)

        confirm_btn = tk.Button(btn_frame, text="🚀 下一步", font=("Segoe UI", 9, "bold"), bg="#4CAF50", fg="white", padx=16, pady=4, command=on_confirm)
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

    def start_mode(self, mode: str = "fork", session_target: str = "last", goal_target: str = ""):
        if mode == "goal" and not goal_target:
            self.prompt_goal_mode(session_target)
            return
        self.controller.start_supervision(mode, session_target, goal_target)

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
        try:
            from afk_supervisor.platform.windows import set_keep_awake
            set_keep_awake(enable=False)
        except Exception:
            pass
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
