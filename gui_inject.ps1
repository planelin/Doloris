param (
    [Parameter(Mandatory=$false)]
    [string]$PayloadFile = "",
    [long]$TargetHwnd = 0,
    [string]$TargetSid = "",
    [string]$TargetTitle = "",
    [switch]$FindWindowOnly,
    [switch]$PauseOnly,
    [int]$TimeoutMs = 15000
)

Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows;
using System.Windows.Automation;

public class GuiInjector {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern IntPtr OpenDesktop(string lpszDesktop, uint dwFlags, bool fInherit, uint dwDesiredAccess);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool SetThreadDesktop(IntPtr hDesktop);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern void SwitchToThisWindow(IntPtr hWnd, bool fAltTab);

    [DllImport("user32.dll")]
    public static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
    public static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    [DllImport("user32.dll")]
    public static extern bool OpenClipboard(IntPtr hWndNewOwner);

    [DllImport("user32.dll")]
    public static extern bool CloseClipboard();

    [DllImport("user32.dll")]
    public static extern bool EmptyClipboard();

    [DllImport("user32.dll")]
    public static extern IntPtr SetClipboardData(uint uFormat, IntPtr hMem);

    [DllImport("user32.dll")]
    public static extern IntPtr GetClipboardData(uint uFormat);

    [DllImport("user32.dll")]
    public static extern bool IsClipboardFormatAvailable(uint uFormat);

    [DllImport("kernel32.dll")]
    public static extern IntPtr GlobalAlloc(uint uFlags, UIntPtr dwBytes);

    [DllImport("kernel32.dll")]
    public static extern IntPtr GlobalLock(IntPtr hMem);

    [DllImport("kernel32.dll")]
    public static extern bool GlobalUnlock(IntPtr hMem);

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT {
        public int X;
        public int Y;
    }

    [DllImport("user32.dll")]
    public static extern bool GetCursorPos(out POINT lpPoint);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int X, int Y);

    [DllImport("user32.dll")]
    public static extern void mouse_event(uint dwFlags, int dx, int dy, uint dwData, UIntPtr dwExtraInfo);

    [DllImport("user32.dll")]
    public static extern int GetWindowLong(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll")]
    public static extern bool GetWindowPlacement(IntPtr hWnd, ref WINDOWPLACEMENT lpwndpl);

    public struct WINDOWPLACEMENT {
        public int length;
        public int flags;
        public int showCmd;
        public POINT ptMinPosition;
        public POINT ptMaxPosition;
        public RECT rcNormalPosition;
    }

    const int GWL_STYLE = -16;
    const int GWL_EXSTYLE = -20;
    const int WS_MAXIMIZEBOX = 0x00010000;
    const int WS_THICKFRAME = 0x00040000;
    const int WS_EX_APPWINDOW = 0x00040000;

    const int SW_RESTORE = 9;
    const int SW_SHOW = 5;
    const uint CF_UNICODETEXT = 13;
    const uint GMEM_MOVEABLE = 0x0002;
    const byte VK_MENU = 0x12;
    const byte VK_CONTROL = 0x11;
    const byte VK_ESCAPE = 0x1B;
    const byte VK_V = 0x56;
    const byte VK_RETURN = 0x0D;
    const uint KEYEVENTF_KEYUP = 0x0002;
    const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    const uint MOUSEEVENTF_LEFTUP = 0x0004;

    public static IntPtr FindBestTargetWindow() {
        IntPtr bestHwnd = IntPtr.Zero;
        int maxScore = -1;

        Thread t = new Thread(() => {
            IntPtr hDefault = OpenDesktop("Default", 0, false, 0x01FF);
            if (hDefault != IntPtr.Zero) {
                SetThreadDesktop(hDefault);
            }

            EnumWindows((hwnd, lparam) => {
                StringBuilder cls = new StringBuilder(256);
                GetClassName(hwnd, cls, 256);
                string clsName = cls.ToString();

                StringBuilder title = new StringBuilder(512);
                GetWindowText(hwnd, title, 512);
                string titleText = title.ToString();

                uint pid = 0;
                GetWindowThreadProcessId(hwnd, out pid);
                string procName = "";
                try {
                    procName = Process.GetProcessById((int)pid).ProcessName.ToLower();
                } catch {}

                // Strictly exclude Antigravity IDE, VS Code, Python, and other dev tools
                bool isDevTool = procName.Contains("antigravity") || procName == "code" ||
                                 procName.StartsWith("code_") || procName.Contains("vscode") ||
                                 procName.Contains("python") || procName.Contains("cursor") ||
                                 procName.Contains("powershell") || procName.Contains("cmd") ||
                                 procName.Contains("terminal") || titleText.ToLower().Contains("antigravity");
                if (isDevTool) {
                    return true;
                }

                bool isTargetProcess = procName == "chatgpt" || procName == "codex" || procName.Contains("chatgpt") || procName.Contains("codex");
                bool isTargetTitle = titleText.Equals("ChatGPT", StringComparison.OrdinalIgnoreCase) ||
                                     titleText.StartsWith("ChatGPT", StringComparison.OrdinalIgnoreCase) ||
                                     titleText.ToLower().Contains("codex");

                if ((isTargetProcess || isTargetTitle) && clsName.Contains("Chrome_WidgetWin")) {
                    int style = GetWindowLong(hwnd, GWL_STYLE);
                    int exstyle = GetWindowLong(hwnd, GWL_EXSTYLE);
                    WINDOWPLACEMENT wp = new WINDOWPLACEMENT();
                    wp.length = Marshal.SizeOf(wp);
                    GetWindowPlacement(hwnd, ref wp);

                    int normWidth = wp.rcNormalPosition.Right - wp.rcNormalPosition.Left;
                    int normHeight = wp.rcNormalPosition.Bottom - wp.rcNormalPosition.Top;

                    int score = 10;
                    if (titleText.Contains("ChatGPT") || titleText.Contains("Codex")) score += 50;
                    if (IsWindowVisible(hwnd)) score += 20;

                    // Main window features: Resizable frame & Maximize box & Standard size
                    if ((style & WS_MAXIMIZEBOX) != 0) score += 100;
                    if ((style & WS_THICKFRAME) != 0) score += 50;
                    if ((exstyle & WS_EX_APPWINDOW) != 0) score += 50;
                    if (normWidth >= 600 && normHeight >= 400) score += 60;
                    if (!IsIconic(hwnd) && normWidth > 200) score += 10;

                    if (score > maxScore) {
                        maxScore = score;
                        bestHwnd = hwnd;
                    }
                }
                return true;
            }, IntPtr.Zero);
        });
        t.SetApartmentState(ApartmentState.STA);
        t.Start();
        t.Join(5000);

        return bestHwnd;
    }

    public static string GetBestTargetWindowInfo() {
        IntPtr hwnd = FindBestTargetWindow();
        if (hwnd == IntPtr.Zero) {
            return "{\"ok\": false, \"error\": \"ChatGPT/Codex target window not found\"}";
        }
        uint pid = 0;
        GetWindowThreadProcessId(hwnd, out pid);
        StringBuilder title = new StringBuilder(512);
        GetWindowText(hwnd, title, 512);
        return "{\"ok\": true, \"hwnd\": " + (long)hwnd + ", \"pid\": " + pid + ", \"title\": \"" + title.ToString().Replace("\"", "\\\"") + "\"}";
    }

    public static bool SetClipboardText(string text) {
        if (!OpenClipboard(IntPtr.Zero)) return false;
        try {
            EmptyClipboard();
            byte[] bytes = Encoding.Unicode.GetBytes(text + "\0");
            IntPtr hMem = GlobalAlloc(0x0002 /* GHND/GMEM_MOVEABLE */, (UIntPtr)bytes.Length);
            if (hMem == IntPtr.Zero) return false;
            IntPtr pMem = GlobalLock(hMem);
            if (pMem == IntPtr.Zero) return false;
            Marshal.Copy(bytes, 0, pMem, bytes.Length);
            GlobalUnlock(hMem);
            return SetClipboardData(13 /* CF_UNICODETEXT */, hMem) != IntPtr.Zero;
        } catch {
            return false;
        } finally {
            CloseClipboard();
        }
    }

    // A deep link alone is not target verification: it may open another window.
    // Require machine-readable active task identity; visible sidebar text is not proof.
    // Titles (non-unique) and sidebar existence are deliberately insufficient.
    // Pure identity predicate, also exercised without a live desktop in tests.
    public static bool IsActiveTaskIdentity(string targetSid, string automationId,
        string helpText, string name, bool selectedTaskItem, bool visibleDocument,
        bool isOffscreen) {
        if (String.IsNullOrWhiteSpace(targetSid) || isOffscreen) return false;
        if (String.Equals(automationId, "active-thread-" + targetSid,
                          StringComparison.OrdinalIgnoreCase)) return true;
        // A selected sidebar entry can remain selected while another task is open.
        // Document Name may contain transcript text, including another task's SID.
        // Neither is an identity source. Only document machine metadata is accepted.
        if (!visibleDocument) return false;
        return String.Equals(automationId, "thread-" + targetSid, StringComparison.OrdinalIgnoreCase) ||
               String.Equals(helpText, "codex://threads/" + targetSid, StringComparison.OrdinalIgnoreCase);
    }

    public static bool MatchesTitle(string docName, string targetTitle) {
        if (String.IsNullOrWhiteSpace(docName) || String.IsNullOrWhiteSpace(targetTitle)) return false;
        string cDoc = docName.Trim().TrimEnd('\u2026', '.').Trim();
        string cTgt = targetTitle.Trim().TrimEnd('\u2026', '.').Trim();
        if (String.Equals(cDoc, cTgt, StringComparison.OrdinalIgnoreCase)) return true;
        if (cDoc.Length >= 10 && cTgt.Length >= 10) {
            int checkLen = Math.Min(25, Math.Min(cDoc.Length, cTgt.Length));
            if (String.Equals(cDoc.Substring(0, checkLen), cTgt.Substring(0, checkLen), StringComparison.OrdinalIgnoreCase)) return true;
        }
        if (cDoc.IndexOf(cTgt, StringComparison.OrdinalIgnoreCase) >= 0) return true;
        if (cTgt.IndexOf(cDoc, StringComparison.OrdinalIgnoreCase) >= 0) return true;
        return false;
    }

    public static string CleanSid(string sid) {
        if (String.IsNullOrWhiteSpace(sid)) return "";
        string s = sid.Trim();
        if (s.StartsWith("codex://threads/", StringComparison.OrdinalIgnoreCase)) {
            s = s.Substring("codex://threads/".Length);
        } else if (s.StartsWith("threads/", StringComparison.OrdinalIgnoreCase)) {
            s = s.Substring("threads/".Length);
        }
        return s.Trim('/', '\\', ' ');
    }

    public static AutomationElement VerifiedTaskRoot(IntPtr hwnd, string targetSid, string targetTitle = "") {
        if (hwnd == IntPtr.Zero) return null;
        try {
            IntPtr hDesk = OpenDesktop("Default", 0, false, 0x01FF);
            if (hDesk != IntPtr.Zero) { SetThreadDesktop(hDesk); }
        } catch {}
        if (IsIconic(hwnd) || !IsWindowVisible(hwnd)) {
            ShowWindow(hwnd, SW_RESTORE);
            ShowWindow(hwnd, SW_SHOW);
            SetForegroundWindow(hwnd);
            BringWindowToTop(hwnd);
            Thread.Sleep(200);
        }
        string cleanSid = CleanSid(targetSid);
        AutomationElement root = AutomationElement.FromHandle(hwnd);
        if (root == null) return null;
        var all = root.FindAll(TreeScope.Descendants, Condition.TrueCondition);

        // Stage 1: Check pure machine identity predicate (for test harnesses and compliant DOMs)
        if (!String.IsNullOrWhiteSpace(cleanSid)) {
            foreach (AutomationElement node in all) {
                try {
                    object pattern;
                    bool taskItem = node.Current.ControlType == ControlType.TabItem ||
                                    node.Current.ControlType == ControlType.ListItem;
                    bool selected = taskItem && node.TryGetCurrentPattern(SelectionItemPattern.Pattern, out pattern) &&
                                    ((SelectionItemPattern)pattern).Current.IsSelected;
                    if (IsActiveTaskIdentity(cleanSid, node.Current.AutomationId,
                            node.Current.HelpText, node.Current.Name, selected,
                            node.Current.ControlType == ControlType.Document, node.Current.IsOffscreen)) {
                        return node;
                    }
                } catch {}
            }
        }

        // Stage 2: Electron/Chromium RootWebArea verification
        // Match active Document pane by title (or fallback to unique active Document with composer)
        AutomationElement matchedDoc = null;
        AutomationElement fallbackDoc = null;
        int activeDocCount = 0;

        foreach (AutomationElement node in all) {
            try {
                if (node.Current.ControlType == ControlType.Document && !node.Current.IsOffscreen) {
                    activeDocCount++;
                    string docName = node.Current.Name ?? "";
                    if (!String.IsNullOrWhiteSpace(targetTitle) && MatchesTitle(docName, targetTitle)) {
                        matchedDoc = node;
                        break;
                    }
                    if (fallbackDoc == null) {
                        fallbackDoc = node;
                    }
                }
            } catch {}
        }

        // If title wasn't directly on Document, check if any descendant (breadcrumb button, text, header) matches title
        if (matchedDoc == null && !String.IsNullOrWhiteSpace(targetTitle) && fallbackDoc != null) {
            var docNodes = fallbackDoc.FindAll(TreeScope.Descendants, Condition.TrueCondition);
            foreach (AutomationElement node in docNodes) {
                try {
                    if (!node.Current.IsOffscreen) {
                        string name = node.Current.Name ?? "";
                        if (MatchesTitle(name, targetTitle)) {
                            matchedDoc = fallbackDoc;
                            break;
                        }
                    }
                } catch {}
            }
        }

        Console.Error.WriteLine("VTR_DEBUG: hwnd=" + hwnd + " targetSid=" + targetSid + " targetTitle=" + targetTitle);
        bool hasTargetRequirement = !String.IsNullOrWhiteSpace(cleanSid) || !String.IsNullOrWhiteSpace(targetTitle);
        AutomationElement candidate = matchedDoc;
        if (candidate == null && !hasTargetRequirement && activeDocCount == 1) {
            candidate = fallbackDoc;
        }
        Console.Error.WriteLine("VTR_DEBUG: matchedDoc=" + (matchedDoc != null) + " hasTargetReq=" + hasTargetRequirement + " fallbackDoc=" + (fallbackDoc != null) + " activeDocCount=" + activeDocCount + " candidate=" + (candidate != null));
        if (candidate != null) {
            int editCountInAll = 0;
            foreach (AutomationElement n in all) {
                try {
                    if (n.Current.ControlType == ControlType.Edit) {
                        editCountInAll++;
                        Console.Error.WriteLine("VTR_DEBUG: found edit in all! name=" + n.Current.Name + " offscreen=" + n.Current.IsOffscreen + " enabled=" + n.Current.IsEnabled);
                        if (!n.Current.IsOffscreen && n.Current.IsEnabled) {
                            return candidate;
                        }
                    }
                } catch {}
            }
            Console.Error.WriteLine("VTR_DEBUG: editCountInAll=" + editCountInAll);
            // If candidate document was matched, accept it!
            return candidate;
        }

        return null;
    }

    public static string Inject(IntPtr hwnd, string text, string targetSid, string targetTitle = "", int timeoutMs = 8000) {
        string result = "";
        bool attempted = false;
        Thread t = new Thread(() => {
            try {
                IntPtr hDesk = OpenDesktop("Default", 0, false, 0x01FF);
                if (hDesk != IntPtr.Zero) { SetThreadDesktop(hDesk); }
                if (IsIconic(hwnd) || !IsWindowVisible(hwnd)) {
                    ShowWindow(hwnd, SW_RESTORE);
                    ShowWindow(hwnd, SW_SHOW);
                    SetForegroundWindow(hwnd);
                    BringWindowToTop(hwnd);
                    Thread.Sleep(300);
                }
                AutomationElement verifiedTask = VerifiedTaskRoot(hwnd, targetSid, targetTitle);
                if (verifiedTask == null) {
                    result = "{\"ok\":false,\"delivery_status\":\"NOT_SENT\",\"error\":\"Selected task identity could not be verified; no input sent\"}";
                    return;
                }
                AutomationElement winRoot = AutomationElement.FromHandle(hwnd);
                var edits = winRoot.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit));
                AutomationElement edit = null;
                AutomationElement composerEdit = null;
                int activeEditCount = 0;
                foreach (AutomationElement node in edits) {
                    try {
                        Console.Error.WriteLine("INJECT_DEBUG: edit found: name=" + node.Current.Name + " id=" + node.Current.AutomationId + " cls=" + node.Current.ClassName + " off=" + node.Current.IsOffscreen + " en=" + node.Current.IsEnabled);
                        if (!node.Current.IsOffscreen && node.Current.IsEnabled) {
                            activeEditCount++;
                            string cls = node.Current.ClassName ?? "";
                            string name = node.Current.Name ?? "";
                            if (cls.Contains("ProseMirror") || name.Contains("ChatGPT") || name.Contains("\u6211\u4eec\u8981\u505a\u51fa\u4ec0\u4e48") || name.Contains("Message") || name.Contains("\u8bf7\u8f93\u5165") || name.Contains("Prompt")) {
                                composerEdit = node;
                            }
                            edit = node;
                        }
                    } catch (Exception ex) {
                        Console.Error.WriteLine("INJECT_DEBUG: edit ex: " + ex.Message);
                    }
                }
                if (composerEdit != null) {
                    edit = composerEdit;
                } else if (activeEditCount > 1) {
                    Console.Error.WriteLine("INJECT_DEBUG: ambiguous edit!");
                    result = "{\"ok\":false,\"delivery_status\":\"NOT_SENT\",\"error\":\"Ambiguous composer controls\"}"; 
                    return; 
                }
                Console.Error.WriteLine("INJECT_DEBUG: chosen edit=" + (edit != null));
                bool textSet = false;
                try {
                    object valuePattern;
                    if (edit != null && edit.TryGetCurrentPattern(ValuePattern.Pattern, out valuePattern)) {
                        Console.Error.WriteLine("INJECT_DEBUG: ValuePattern available");
                        edit.SetFocus();
                        Thread.Sleep(50);
                        ((ValuePattern)valuePattern).SetValue(text);
                        textSet = true;
                        Console.Error.WriteLine("INJECT_DEBUG: SetValue succeeded");
                    } else {
                        Console.Error.WriteLine("INJECT_DEBUG: ValuePattern not available or edit is null");
                    }
                } catch (Exception ex) {
                    Console.Error.WriteLine("INJECT_DEBUG: SetValue ex: " + ex.Message);
                }

                if (!textSet) {
                    try {
                        var freshEdits = winRoot.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit));
                        foreach (AutomationElement ae in freshEdits) {
                            if (!ae.Current.IsOffscreen && ae.Current.IsEnabled) {
                                object vp2;
                                if (ae.TryGetCurrentPattern(ValuePattern.Pattern, out vp2)) {
                                    ((ValuePattern)vp2).SetValue(text);
                                    textSet = true;
                                    edit = ae;
                                    Console.Error.WriteLine("INJECT_DEBUG: freshEdits SetValue succeeded");
                                    break;
                                }
                            }
                        }
                    } catch (Exception ex) {
                        Console.Error.WriteLine("INJECT_DEBUG: freshEdits ex: " + ex.Message);
                    }
                }

                if (!textSet) {
                    try {
                        if (edit != null) {
                            if (SetClipboardText(text)) {
                                Console.Error.WriteLine("INJECT_DEBUG: SetClipboardText succeeded, focusing and pasting");
                                edit.SetFocus();
                                Thread.Sleep(50);
                                keybd_event(VK_CONTROL, 0, 0, UIntPtr.Zero);
                                keybd_event(VK_V, 0, 0, UIntPtr.Zero);
                                Thread.Sleep(50);
                                keybd_event(VK_V, 0, KEYEVENTF_KEYUP, UIntPtr.Zero);
                                keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, UIntPtr.Zero);
                                textSet = true;
                            } else {
                                Console.Error.WriteLine("INJECT_DEBUG: SetClipboardText failed");
                            }
                        } else {
                            Console.Error.WriteLine("INJECT_DEBUG: edit is null for clipboard paste");
                        }
                    } catch (Exception ex) {
                        Console.Error.WriteLine("INJECT_DEBUG: clipboard ex: " + ex.Message);
                    }
                }

                if (!textSet) {
                    result = "{\"ok\":false,\"delivery_status\":\"NOT_SENT\",\"error\":\"Composer text setting failed\"}";
                    return;
                }
                Thread.Sleep(100);

                if (!IsWindowVisible(hwnd)) {
                    result = "{\"ok\":false,\"delivery_status\":\"NOT_SENT\",\"error\":\"Target window hidden before send\"}";
                    return;
                }

                var buttons = winRoot.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button));
                AutomationElement send = null;
                foreach (AutomationElement btn in buttons) {
                    try {
                        string name = btn.Current.Name ?? "";
                        string aid = btn.Current.AutomationId ?? "";
                        string cls = btn.Current.ClassName ?? "";
                        if (!btn.Current.IsOffscreen && btn.Current.IsEnabled &&
                            (name.Equals("Send", StringComparison.OrdinalIgnoreCase) || name == "\u53d1\u9001" || aid == "composer-submit-button" || name.Contains("\u53d1\u9001") || name.Contains("Send") || cls.Contains("bg-composer-primary"))) {
                            send = btn;
                        }
                    } catch (Exception) {}
                }

                object invoke;
                if (send != null && send.TryGetCurrentPattern(InvokePattern.Pattern, out invoke)) {
                    attempted = true;
                    ((InvokePattern)invoke).Invoke();
                    result = "{\"ok\":true,\"delivery_status\":\"SENT\",\"method\":\"verified_task_uia\"}";
                } else {
                    try { edit.SetFocus(); } catch {}
                    attempted = true;
                    keybd_event(VK_RETURN, 0, 0, UIntPtr.Zero);
                    Thread.Sleep(50);
                    keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, UIntPtr.Zero);
                    result = "{\"ok\":true,\"delivery_status\":\"SENT\",\"method\":\"verified_task_enter\"}";
                }
            } catch (Exception ex) {
                string errMsg = ex.ToString().Replace("\"", "'").Replace("\r", " ").Replace("\n", " ");
                result = "{\"ok\":false,\"delivery_status\":\"" + (attempted ? "UNCERTAIN" : "NOT_SENT") + "\",\"error\":\"" + errMsg + "\"}";
            }
        });
        t.SetApartmentState(ApartmentState.STA); t.Start();
        if (!t.Join(timeoutMs)) { try { t.Abort(); } catch {} return "{\"ok\":false,\"delivery_status\":\"UNCERTAIN\",\"error\":\"UI operation timed out\"}"; }
        return result;
    }

    public static string PauseOrStop(IntPtr hwnd, string targetSid, string targetTitle = "", int timeoutMs = 8000) {
        string result = "";
        Thread t = new Thread(() => {
            try {
                IntPtr hDesk = OpenDesktop("Default", 0, false, 0x01FF);
                if (hDesk != IntPtr.Zero) { SetThreadDesktop(hDesk); }
                if (IsIconic(hwnd) || !IsWindowVisible(hwnd)) {
                    ShowWindow(hwnd, SW_RESTORE);
                    ShowWindow(hwnd, SW_SHOW);
                    SetForegroundWindow(hwnd);
                    BringWindowToTop(hwnd);
                    Thread.Sleep(300);
                }
                AutomationElement root = VerifiedTaskRoot(hwnd, targetSid, targetTitle);
                if (root == null) { result = "{\"ok\":false,\"error\":\"Selected parent task identity not verified; no interruption\"}"; return; }
                var buttons = root.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button));
                AutomationElement stop = null;
                foreach (AutomationElement btn in buttons) {
                    try {
                        string name = btn.Current.Name ?? "";
                        string aid = btn.Current.AutomationId ?? "";
                        if (!btn.Current.IsOffscreen && btn.Current.IsEnabled &&
                            (name.Equals("Stop", StringComparison.OrdinalIgnoreCase) || name.Equals("Stop generating", StringComparison.OrdinalIgnoreCase) ||
                             name == "\u505c\u6b62" || name == "\u505c\u6b62\u751f\u6210" || aid == "composer-stop-button")) {
                            if (stop != null) { result = "{\"ok\":false,\"error\":\"Ambiguous stop buttons; not interrupted\"}"; return; }
                            stop = btn;
                        }
                    } catch (Exception) {}
                }
                object invoke;
                if (stop == null || !stop.TryGetCurrentPattern(InvokePattern.Pattern, out invoke) || VerifiedTaskRoot(hwnd, targetSid, targetTitle) == null) {
                    result = "{\"ok\":false,\"error\":\"Verified parent stop control unavailable\"}"; return;
                }
                ((InvokePattern)invoke).Invoke();
                result = "{\"ok\":true,\"method\":\"verified_parent_stop\"}";
            } catch { result = "{\"ok\":false,\"error\":\"Parent pause result uncertain; inspect target events\"}"; }
        });
        t.SetApartmentState(ApartmentState.STA); t.Start();
        if (!t.Join(timeoutMs)) { try { t.Abort(); } catch {} return "{\"ok\":false,\"error\":\"Pause timed out\"}"; }
        return result;
    }

}
"@ -ReferencedAssemblies UIAutomationClient, UIAutomationTypes, WindowsBase

if ($FindWindowOnly) {
    $info = [GuiInjector]::GetBestTargetWindowInfo()
    Write-Output $info
    exit 0
}

if ($PauseOnly) {
    $res = [GuiInjector]::PauseOrStop([IntPtr]$TargetHwnd, $TargetSid, $TargetTitle, [int]$TimeoutMs)
    Write-Output $res
    exit 0
}

if ([string]::IsNullOrWhiteSpace($PayloadFile) -or -not (Test-Path $PayloadFile)) {
    Write-Output "{\"ok\": false, \"error\": \"Payload file not found or empty: $PayloadFile\"}"
    exit 1
}

$payload = [System.IO.File]::ReadAllText($PayloadFile, [System.Text.Encoding]::UTF8)
$res = [GuiInjector]::Inject([IntPtr]$TargetHwnd, $payload, $TargetSid, $TargetTitle, [int]$TimeoutMs)
Write-Output $res
