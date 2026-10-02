"""
afk_supervisor.platform.windows — Windows 系统级交互与防睡眠/网络控制
===================================================================
通过 Win32 API 抑制系统休眠；动态检测系统级/注册表代理；管理网络适配器测试环境。
"""

import os
import queue
import subprocess
import sys
import threading
import urllib.request
from typing import Optional, Tuple

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0


# Windows 电源执行状态标志位
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# SetThreadExecutionState(ES_CONTINUOUS) 是线程亲和的: 设置与清除必须发生在
# 同一个线程, 否则跨线程清除无效、防熄屏永不解除 (桌宠主线程 set / worker
# 线程 clear 的真实场景)。因此所有请求都排队到这个专属 owner 线程执行。
_ka_lock = threading.Lock()
_ka_thread: Optional[threading.Thread] = None
_ka_commands: Optional["queue.Queue"] = None
_ka_ready: Optional[threading.Event] = None
_ka_result = [False]


def _keep_awake_owner() -> None:
    """防熄屏 owner 线程: 同一线程内执行全部 set/clear, 退出前负责解除。"""
    import ctypes
    while True:
        cmd, flags = _ka_commands.get()
        if cmd == "stop":
            break
        _ka_result[0] = False
        try:
            res = ctypes.windll.kernel32.SetThreadExecutionState(flags)
            if res == 0:
                print("[WARN] SetThreadExecutionState 返回 0，防熄屏可能未生效", file=sys.stderr)
            else:
                _ka_result[0] = True
                tag = "系统+显示器不熄屏" if flags & ES_DISPLAY_REQUIRED else "仅系统不休眠(允许熄屏)"
                print(f"[INFO] KEEP-AWAKE 电源状态更新: {tag}")
        except Exception as e:
            print(f"[WARN] 睡眠抑制设置失败: {e}", file=sys.stderr)
        finally:
            _ka_ready.set()
    # 线程退出前必须在自己身上解除执行状态
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


def set_keep_awake(enable: bool = True, keep_display: bool = True) -> bool:
    """设置 Windows 系统及显示器防休眠/防熄屏状态 (线程安全)。

    :param enable: True 为保持活跃，False 为恢复系统默认电源设置
    :param keep_display: True 时同时阻止显示器熄屏 (ES_DISPLAY_REQUIRED)，False 允许熄屏
    :return: 是否设置成功
    """
    global _ka_thread, _ka_commands, _ka_ready
    if sys.platform != "win32":
        return False
    with _ka_lock:
        if enable:
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            if keep_display:
                flags |= ES_DISPLAY_REQUIRED
            if _ka_thread is None or not _ka_thread.is_alive():
                _ka_commands = queue.Queue()
                _ka_ready = threading.Event()
                _ka_thread = threading.Thread(target=_keep_awake_owner,
                                              name="doloris-keep-awake", daemon=True)
                _ka_thread.start()
            _ka_ready.clear()
            _ka_commands.put(("set", flags))
            if not _ka_ready.wait(3.0):
                return False
            return _ka_result[0]
        # disable: 幂等——无活跃请求时直接成功
        if _ka_thread is None or not _ka_thread.is_alive():
            return True
        _ka_commands.put(("stop", None))
        _ka_thread.join(timeout=3.0)
        if _ka_thread.is_alive():
            return False
        _ka_thread = None
        _ka_commands = None
        _ka_ready = None
        return True


def keep_awake(keep_display: bool = True):
    """阻止 Windows 系统进入休眠，默认同时阻止显示器熄屏。
    通过 set_keep_awake 实现向后兼容。
    """
    set_keep_awake(enable=True, keep_display=keep_display)

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


def ps_single_quoted(value: str) -> str:
    """把任意字符串安全地嵌入 PowerShell 单引号字面量 (单引号自身需翻倍)。"""
    return "'" + str(value).replace("'", "''") + "'"


def net_disable(adapter_name: str) -> bool:
    """禁用指定网络适配器 (用于 Chaos 实验)。"""
    cmd = f"Disable-NetAdapter -Name {ps_single_quoted(adapter_name)} -Confirm:$false"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, errors="replace", timeout=15,
                           creationflags=NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


def net_enable(adapter_name: str) -> bool:
    """启用指定网络适配器。"""
    cmd = f"Enable-NetAdapter -Name {ps_single_quoted(adapter_name)} -Confirm:$false"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, errors="replace", timeout=15,
                           creationflags=NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False
