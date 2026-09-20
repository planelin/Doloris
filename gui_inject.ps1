param (
    [Parameter(Mandatory=$false)]
    [string]$PayloadFile = "",
    [long]$TargetHwnd = 0,
    [string]$TargetSid = "",
    [string]$TargetTitle = "",
    [switch]$FindWindowOnly,
    [switch]$PauseOnly,
    [int]$TimeoutMs = 8000
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
        IntPtr hDefault = OpenDesktop("Default", 0, false, 0x01FF);
        if (hDefault != IntPtr.Zero) {
            SetThreadDesktop(hDefault);
        }

        IntPtr bestHwnd = IntPtr.Zero;
        int maxScore = -1;

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

    public static AutomationElement VerifiedTaskRoot(IntPtr hwnd, string targetSid, string targetTitle = "") {
        if (hwnd == IntPtr.Zero || String.IsNullOrWhiteSpace(targetSid)) return null;
        AutomationElement root = AutomationElement.FromHandle(hwnd);
        if (root == null) return null;
        var all = root.FindAll(TreeScope.Descendants, Condition.TrueCondition);
        AutomationElement verified = null;
        foreach (AutomationElement node in all) {
            try {
                object pattern;
                bool taskItem = node.Current.ControlType == ControlType.TabItem ||
                                node.Current.ControlType == ControlType.ListItem;
                bool selected = taskItem && node.TryGetCurrentPattern(SelectionItemPattern.Pattern, out pattern) &&
                                ((SelectionItemPattern)pattern).Current.IsSelected;
                if (IsActiveTaskIdentity(targetSid, node.Current.AutomationId,
                        node.Current.HelpText, node.Current.Name, selected,
                        node.Current.ControlType == ControlType.Document, node.Current.IsOffscreen)) {
                    // Scope composer/stop lookup to the proven task pane, never the
                    // whole window (which can also contain another task's composer).
                    if (verified != null) return null;
                    verified = node;
                }
            } catch {}
        }
        return verified;
    }

    public static string Inject(IntPtr hwnd, string text, string targetSid, string targetTitle = "", int timeoutMs = 8000) {
        string result = "";
        bool attempted = false;
        Thread t = new Thread(() => {
            try {
                IntPtr hDesk = OpenDesktop("Default", 0, false, 0x01FF);
                if (hDesk != IntPtr.Zero) { SetThreadDesktop(hDesk); }
                if (IsIconic(hwnd)) { ShowWindow(hwnd, SW_RESTORE); Thread.Sleep(200); }
                AutomationElement root = VerifiedTaskRoot(hwnd, targetSid, targetTitle);
                if (root == null) {
                    result = "{\"ok\":false,\"delivery_status\":\"NOT_SENT\",\"error\":\"Selected task identity could not be verified; no input sent\"}";
                    return;
                }
                var edits = root.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit));
                AutomationElement edit = null;
                foreach (AutomationElement node in edits) {
                    try {
                        if (!node.Current.IsOffscreen && node.Current.IsEnabled) {
                            if (edit != null) { result = "{\"ok\":false,\"delivery_status\":\"NOT_SENT\",\"error\":\"Ambiguous composer controls\"}"; return; }
                            edit = node;
                        }
                    } catch (Exception) {}
                }
                if (edit == null) { result = "{\"ok\":false,\"delivery_status\":\"NOT_SENT\",\"error\":\"No composer\"}"; return; }
                object valuePattern;
                if (!edit.TryGetCurrentPattern(ValuePattern.Pattern, out valuePattern)) {
                    result = "{\"ok\":false,\"delivery_status\":\"NOT_SENT\",\"error\":\"Composer lacks safe ValuePattern; no blind keyboard fallback\"}"; return;
                }
                var vp = (ValuePattern)valuePattern;
                edit.SetFocus();
                Thread.Sleep(50);
                vp.SetValue(text);
                Thread.Sleep(200);

                if (VerifiedTaskRoot(hwnd, targetSid, targetTitle) == null) {
                    result = "{\"ok\":false,\"delivery_status\":\"NOT_SENT\",\"error\":\"Target changed before send\"}";
                    return;
                }

                var buttons = root.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button));
                AutomationElement send = null;
                foreach (AutomationElement btn in buttons) {
                    try {
                        string name = btn.Current.Name ?? "";
                        string aid = btn.Current.AutomationId ?? "";
                        if (!btn.Current.IsOffscreen && btn.Current.IsEnabled &&
                            (name.Equals("Send", StringComparison.OrdinalIgnoreCase) || name == "\u53d1\u9001" || aid == "composer-submit-button" || name.Contains("\u53d1\u9001") || name.Contains("Send"))) {
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
                    attempted = true;
                    edit.SetFocus();
                    Thread.Sleep(50);
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
                if (IsIconic(hwnd)) { ShowWindow(hwnd, SW_RESTORE); Thread.Sleep(200); }
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
