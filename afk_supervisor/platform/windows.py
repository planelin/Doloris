"""
afk_supervisor.platform.windows — Windows 系统级交互与防睡眠/网络控制
===================================================================
通过 Win32 API 抑制系统休眠；动态检测系统级/注册表代理；管理网络适配器测试环境。
"""

import os
import re
import subprocess
import sys
import urllib.request
from typing import Optional, Tuple

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0


def keep_awake():
    """阻止 Windows 系统进入休眠（允许关闭显示器）。
    通过 SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) 实现进程级防睡眠。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        res = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        if res == 0:
            print("[WARN] SetThreadExecutionState 返回 0，睡眠抑制可能未生效", file=sys.stderr)
        else:
            print("[INFO] KEEP-AWAKE 睡眠抑制已生效(进程级, 允许熄屏)")
    except Exception as e:
        print(f"[WARN] 睡眠抑制设置失败: {e}", file=sys.stderr)

    # 验证 powercfg 权限
    try:
        r = subprocess.run(
            ["powercfg", "/requests"],
            capture_output=True, text=True, errors="replace", timeout=5,
            creationflags=NO_WINDOW
        )
        if r.returncode == 0:
            print("[INFO] powercfg /requests 可读(管理员), 系统请求清单已可核查")
    except Exception:
        pass


def detect_system_proxy() -> Optional[str]:
    """检测当前系统的有效 HTTP 代理 (环境变量优先，其次查注册表)。"""
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = os.environ.get(var)
        if val:
            return val

    # 查注册表 (Windows 专用)
    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            )
            enabled, _ = winreg.QueryValueEx(k, "ProxyEnable")
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
            winreg.CloseKey(k)
            if enabled and server:
                if not server.startswith("http://") and not server.startswith("https://"):
                    server = "http://" + server
                return server
        except Exception:
            pass

    # 最后尝试标准库
    proxies = urllib.request.getproxies()
    return proxies.get("http") or proxies.get("https")


def find_connected_adapter() -> Tuple[Optional[str], Optional[str]]:
    """查找当前已连接且有默认网关的网络适配器 (Name, InterfaceDescription)。"""
    cmd = "Get-NetAdapter | Where-Object { $_.Status -eq 'Up' } | Select-Object -First 1 -Property Name, InterfaceDescription | ConvertTo-Json"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, errors="replace", timeout=10,
                           creationflags=NO_WINDOW)
        import json
        obj = json.loads(r.stdout.strip())
        return obj.get("Name"), obj.get("InterfaceDescription")
    except Exception:
        return None, None


def net_disable(adapter_name: str) -> bool:
    """禁用指定网络适配器 (用于 Chaos 实验)。"""
    cmd = f"Disable-NetAdapter -Name '{adapter_name}' -Confirm:$false"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, errors="replace", timeout=15,
                           creationflags=NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


def net_enable(adapter_name: str) -> bool:
    """启用指定网络适配器。"""
    cmd = f"Enable-NetAdapter -Name '{adapter_name}' -Confirm:$false"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, errors="replace", timeout=15,
                           creationflags=NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False
