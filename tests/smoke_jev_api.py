"""P6 可选真实 Jev API Smoke Test (仅在本机存在 DOLORIS_JEV_TOKEN 时执行)。

用途:
- 验证与 MindsHub Jev API 的 HTTP 通路;
- 验证真实响应可被决策层解析 (choice / noul);
- 打印脱敏后的结构化结果。

边界 (pipeline §P6):
- 只发送下方固定的无敏感信息样例, 不接入真实 Codex 任务;
- 不读写工作区文件; Token 只从环境变量读取, 绝不打印;
- 未配置 Token 时明确跳过并退出 0, 不伪造成功。

用法:
    python -B -X utf8 tests/smoke_jev_api.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from afk_supervisor.decisions.jev import JevConfig, JevProvider
from afk_supervisor.decisions.models import DecisionRequest

SMOKE_SAMPLE_STATE = {
    "report": "The outer box arrived torn. The item inside is undamaged and works normally.",
}

SMOKE_SAMPLE_QUESTIONS = {
    "team": {
        "type": "choice",
        "instructions": "Which team should review this delivery report?",
        "criteria": {
            "packaging": "Damage to the packaging, with the item itself intact.",
            "product": "Damage to the item itself.",
            "other": "A different issue, or not enough information to identify one.",
        },
    },
    "replacement": {
        "type": "noul",
        "instructions": "Does the report explicitly ask for a replacement item?",
    },
}


def main() -> int:
    token_present = bool(os.environ.get("DOLORIS_JEV_TOKEN"))
    if not token_present:
        print("SMOKE SKIP: 未配置 DOLORIS_JEV_TOKEN, 跳过真实 API Smoke Test (不伪造成功)。")
        print("配置 Token 后运行: python -B -X utf8 tests/smoke_jev_api.py")
        return 0

    config = JevConfig.from_env()
    provider = JevProvider(config=config)
    request = DecisionRequest(
        request_id="req-smoke-jev-api",
        state=SMOKE_SAMPLE_STATE,
        questions=SMOKE_SAMPLE_QUESTIONS,
        metadata={"source": "doloris-smoke-test"},
    )
    result = provider.decide(request)

    print("SMOKE RESULT (脱敏):")
    print(json.dumps({
        "provider": result.provider,
        "model": result.model,
        "request_id": result.request_id,
        "status": result.status,
        "error_code": result.error_code,
        "latency_ms": result.latency_ms,
        "answers": result.answers,
        "raw_metadata": result.raw_metadata,
    }, ensure_ascii=False, indent=2))

    if result.status == "OK":
        print("SMOKE PASS: HTTP 通路与响应解析均正常。")
        return 0
    print("SMOKE FAIL: Jev API 调用未成功, 见上方结构化 status/error_code。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
