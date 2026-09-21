"""Opt-in functional execution, not ingestion of worker-authored 'PASS' files.

Only a plan explicitly selected at startup is executed. Shell/prose/log discovery
is deliberately absent. The plan is pinned; outputs are captured by L1. A restart
reruns checks instead of trusting persisted result JSON from the shared workspace.
This runner is NOT an OS sandbox: selecting a plan authorizes its programs/code.
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from afk_supervisor.models import EvidenceItem
from afk_supervisor.storage import atomic_json

KINDS = {"functional", "unit_test", "integration_test", "browser_test", "cli_test"}


def validate_plan(data, baseline):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("verification plan requires version=1")
    checks = data.get("checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 20:
        raise ValueError("verification plan requires 1..20 checks")
    ids = set()
    criteria = {c["id"] for c in baseline.required_criteria}
    root = Path(baseline.session_cwd or baseline.delivery_dir).resolve()
    delivery = Path(baseline.delivery_dir).resolve()
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("invalid check")
        cid = check.get("id", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", cid) or cid in ids:
            raise ValueError("duplicate/invalid check id")
        ids.add(cid)
        if check.get("kind") not in KINDS:
            raise ValueError("check requires an execution verification kind")
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a and "\x00" not in a for a in argv):
            raise ValueError("argv must be a nonempty string array, never shell text")
        if Path(argv[0]).name.lower() in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "sh", "bash"} or Path(argv[0]).suffix.lower() in {".bat", ".cmd", ".ps1"}:
            raise ValueError("use a direct test executable, not a shell wrapper")
        cwd = (root / check.get("cwd", ".")).resolve()
        if not any(cwd == p or p in cwd.parents for p in (root, delivery)):
            raise ValueError("verification cwd escapes the authorized task directories")
        mapped = check.get("criterion_ids")
        if not isinstance(mapped, list) or not mapped or not all(c in criteria for c in mapped):
            raise ValueError("check must map to existing task criterion IDs")
        timeout = check.get("timeout_sec", 120)
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 1800:
            raise ValueError("timeout_sec must be in (0, 1800]")
        expect = check.get("expect", {})
        if not isinstance(expect, dict) or not isinstance(expect.get("exit_code", 0), int):
            raise ValueError("invalid check expectation")
        if not isinstance(expect.get("stdout_contains", []), list) or not all(isinstance(v, str) and v for v in expect.get("stdout_contains", [])):
            raise ValueError("stdout_contains must be a string array")
    return data


def pin_plan(source, run_dir, baseline):
    source = Path(source).resolve()
    raw = source.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    validate_plan(data, baseline)
    pinned = Path(run_dir) / "verification-plan.json"
    # A worker cannot change the selected commands midway through a run.
    pinned.write_bytes(raw)
    return {"path": str(pinned), "sha256": hashlib.sha256(raw).hexdigest(), "source": str(source)}


class VerificationRunner:
    def __init__(self, run_dir, approved_plan=None, budget=None):
        self.run_dir = Path(run_dir)
        self.approved_plan = approved_plan or {}
        self.budget = budget
        self.cache = None

    def invalidate(self):
        self.cache = None

    def _plan(self, baseline):
        raw = Path(self.approved_plan["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.approved_plan["sha256"]:
            raise ValueError("pinned verification plan changed; refuse execution")
        return validate_plan(json.loads(raw.decode("utf-8-sig")), baseline)

    def collect(self, artifact_revision, baseline, current_revision):
        functional = [c["id"] for c in baseline.required_criteria if c.get("type") == "functional"]
        if not self.approved_plan:
            if not functional:
                return [], []
            return [EvidenceItem(
                "ev_functional_plan_missing", "runtime_check", "缺少已授权的功能验证计划",
                details="尚未执行功能检查；必须在启动时用 --verification-plan 指定测试计划。不得把 Worker 自述、语法通过或自写成功 JSON 当成功证据。L2 应说明缺失证据并决定有权限的替代方案或停止。",
                verification_kind="functional", status="UNKNOWN", artifact_revision=artifact_revision,
                criterion_ids=functional,
            )], []
        try:
            plan = self._plan(baseline)
        except (OSError, ValueError, KeyError) as error:
            return [], [f"功能验证计划不可用: {error}"]
        if self.cache and self.cache[0] == artifact_revision:
            return self.cache[1], self.cache[2]
        items, failures = [], []
        for check in plan["checks"]:
            timeout = check.get("timeout_sec", 120)
            if self.budget:
                timeout = self.budget.bound_timeout(timeout)
            cwd = (Path(baseline.session_cwd or baseline.delivery_dir) / check.get("cwd", ".")).resolve()
            directory = self.run_dir / "verification" / artifact_revision / check["id"]
            directory.mkdir(parents=True, exist_ok=True)
            stdout, stderr = directory / "stdout.txt", directory / "stderr.txt"
            record = {"argv": check["argv"], "cwd": str(cwd), "artifact_revision": artifact_revision,
                      "plan_sha256": self.approved_plan["sha256"], "started_at": time.time(),
                      "exit_code": None, "timed_out": False, "error": ""}
            status = "FAIL"
            process = None
            try:
                if timeout <= 0:
                    raise TimeoutError("overall verification deadline exhausted")
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
                env["AFK_VERIFICATION_OUTPUT_DIR"] = str(directory)
                options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                with stdout.open("wb") as out, stderr.open("wb") as err:
                    process = subprocess.Popen(check["argv"], cwd=str(cwd), stdin=subprocess.DEVNULL,
                                               stdout=out, stderr=err, env=env, shell=False, **options)
                    try:
                        record["exit_code"] = process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        record["timed_out"] = True
                        # Only the process tree this verifier just launched is owned.
                        if os.name == "nt":
                            no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10, creationflags=no_win)
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                        process.kill()
                        process.wait(timeout=5)
                expected = check.get("expect", {})
                output = stdout.read_text(encoding="utf-8", errors="replace")
                status = "PASS" if (not record["timed_out"] and record["exit_code"] == expected.get("exit_code", 0)
                                    and all(s in output for s in expected.get("stdout_contains", []))) else "FAIL"
            except Exception as error:
                record["error"] = f"{type(error).__name__}: {error}"
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            record["finished_at"] = time.time()
            record["status"] = status
            record["stdout"] = str(stdout)
            record["stderr"] = str(stderr)
            record["stdout_sha256"] = hashlib.sha256(stdout.read_bytes()).hexdigest() if stdout.exists() else ""
            record["stderr_sha256"] = hashlib.sha256(stderr.read_bytes()).hexdigest() if stderr.exists() else ""
            atomic_json(directory / "execution.json", record)
            # Timestamps stay in the audit receipt, not in the review content hash.
            facts = {k: v for k, v in record.items() if k not in {"started_at", "finished_at"}}
            for key, file in (("stdout_tail", stdout), ("stderr_tail", stderr)):
                if file.exists():
                    with file.open("rb") as stream:
                        stream.seek(max(0, file.stat().st_size - 16000))
                        facts[key] = stream.read().decode("utf-8", errors="replace")
            item = EvidenceItem(
                "ev_exec_" + check["id"], "verification_result", f"实际执行 {check['id']}: {status}",
                details=json.dumps(facts, ensure_ascii=False, sort_keys=True), path=str(directory / "execution.json"),
                verification_kind=check["kind"], status=status, artifact_revision=artifact_revision,
                criterion_ids=check["criterion_ids"],
            )
            items.append(item)
            if status != "PASS":
                failures.append(f"功能验证失败: {check['id']} (exit={record['exit_code']}, timeout={record['timed_out']}, error={record['error']})")
        if current_revision() != artifact_revision:
            failures.append("验证执行期间产物发生变化；结果已失效，必须针对新版本重新执行与审查")
            for item in items:
                item.status = "FAIL"
        self.cache = artifact_revision, items, failures
        return items, failures
