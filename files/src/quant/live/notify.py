"""pushplus 推送。

约束（踩过才知道的坑）
----------------------
- token 走环境变量 ``PUSHPLUS_TOKEN``（GitHub Secrets），**不要**写进 config；
- 免费版对正文长度有限制，正文按 markdown 组织、控制在 2000 字内，超长会被截断或退回；
- 返回 HTTP 200 不代表推送成功，必须看响应体里的 ``code``（成功是 200）；
- 推送失败是最危险的失败：没人知道信号没到手机。所以失败时除了重试，还要打印
  GitHub Actions 的 ``::error::`` 注解让 job 直接标红，并把原文写进日志。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

log = logging.getLogger(__name__)

API = "https://www.pushplus.plus/send"
MAX_CONTENT = 2000


def token() -> str:
    return os.getenv("PUSHPLUS_TOKEN", "").strip()


def _post(payload: dict, timeout: float = 15.0) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "trade-agent/0.1",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def send(title: str, content: str, retries: int = 3) -> bool:
    """推一条消息。返回是否真的推成功（以响应体 code 为准，不看 HTTP 状态）。"""
    tk = token()
    if not tk:
        log.error("PUSHPLUS_TOKEN 未设置，无法推送")
        print("::error::PUSHPLUS_TOKEN 未设置，模拟盘信号没有送达")
        return False

    if len(content) > MAX_CONTENT:
        content = content[: MAX_CONTENT - 20] + "\n\n…（正文已截断）"

    payload = {"token": tk, "title": title[:100], "content": content,
               "template": "markdown", "channel": "wechat"}

    for attempt in range(1, retries + 1):
        try:
            resp = _post(payload)
            code = resp.get("code")
            if code == 200:
                log.info("pushplus 推送成功：%s", title)
                return True
            log.warning("pushplus 第 %d 次返回 code=%s msg=%s", attempt, code, resp.get("msg"))
        except Exception as exc:
            log.warning("pushplus 第 %d 次请求异常：%s", attempt, type(exc).__name__)
        if attempt < retries:
            time.sleep(2.0 * attempt)

    print(f"::error::pushplus 推送失败（已重试 {retries} 次）：{title}")
    log.error("推送失败，正文如下供日志留存：\n%s", content)
    return False
