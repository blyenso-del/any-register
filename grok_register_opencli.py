#!/usr/bin/env python3
"""
Grok 注册机 (opencli browser bridge 版)

用 opencli 连接用户已运行的 Chrome 完成 xAI 账号注册，替代 DrissionPage 独立 Chromium。
参照 E:\\ai-stock\\scrape_jiuyan_opencli.py 的 opencli 调用模式。

流程:
  1. 打开 accounts.x.ai/sign-up → 点击「使用邮箱注册」
  2. 创建临时邮箱 → 填入 → 提交
  3. 轮询邮箱获取验证码 → 填入 → 提交
  4. 填写姓名/密码 → 等待 Cloudflare Turnstile → 提交
  5. 检测注册成功（URL 跳转 grok.com）→ 间接获取 sso cookie
  6. (可选) HTTP 开启 NSFW
  7. 保存账号

用法:
  python grok_register_opencli.py              # 使用 config.json 配置
  python grok_register_opencli.py -n 1         # 注册 5 个
  python grok_register_opencli.py --no-nsfw    # 跳过 NSFW
  python grok_register_opencli.py --provider cloudflare
  python grok_register_opencli.py --debug

前置:
  - opencli browser bridge 已连接 (opencli doctor)
  - Chrome 已安装 turnstilePatch 扩展（或等效 Turnstile 自动通过方案）
  - config.json 中配置了邮箱 provider 及对应 API key

产物:
  accounts_YYYYMMDD_HHMMSS.txt  (email----password----sso)
  mail_credentials.txt          (email\\tcredential)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import shutil
import string
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# ─── 常量 ───────────────────────────────────────────────────────────────────────

SESSION = "grok_register"
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

DUCKMAIL_API_BASE = "https://api.duckmail.sbs"
YYDS_API_BASE = "https://maliapi.215.im/v1"

BRIDGE_RECOVER_RETRIES = 3

# ─── opencli 基础设施 ───────────────────────────────────────────────────────────


class BridgeDisconnectedError(RuntimeError):
    """opencli Browser Bridge 断开。"""


def _find_opencli_main() -> list[str]:
    npm = (
        Path(os.environ.get("APPDATA", ""))
        / "npm"
        / "node_modules"
        / "@jackwener"
        / "opencli"
        / "dist"
        / "src"
        / "main.js"
    )
    if npm.is_file():
        node = shutil.which("node")
        if node:
            return [node, str(npm)]

    for name in ("opencli", "opencli.cmd"):
        p = shutil.which(name)
        if not p:
            continue
        if p.lower().endswith(".cmd"):
            sibling = (
                Path(p).parent
                / "node_modules"
                / "@jackwener"
                / "opencli"
                / "dist"
                / "src"
                / "main.js"
            )
            if sibling.is_file() and shutil.which("node"):
                return [shutil.which("node"), str(sibling)]
        return [p]

    raise RuntimeError("找不到 opencli，请先 npm i -g @jackwener/opencli")


OPENCLI = _find_opencli_main()


def _opencli_env() -> dict[str, str]:
    env = os.environ.copy()
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = env["NO_PROXY"]
    return env


def _is_bridge_error(msg: str) -> bool:
    m = (msg or "").lower()
    return (
        "browser bridge extension not connected" in m
        or "extension not connected" in m
        or "extension: not connected" in m
    )


def run(*args: str, timeout: int = 90) -> str:
    env = _opencli_env()
    cmd = [*OPENCLI, "browser", SESSION, *args]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"opencli 超时 ({timeout}s): {args[0] if args else ''}") from e
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0 and not out:
        msg = f"opencli 失败 rc={r.returncode}: {err or out}"
        if _is_bridge_error(msg):
            raise BridgeDisconnectedError(msg)
        raise RuntimeError(msg)
    return out


def eval_js(js: str, timeout: int = 90) -> str:
    return run("eval", js, timeout=timeout)


def extract_json(text: str):
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    text = re.sub(r"^```(?:json)?", "", text)
    text = re.sub(r"```$", "", text.strip())
    for pat in (r"\[[\s\S]*\]", r"\{[\s\S]*\}"):
        m = re.search(pat, text)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                continue
    return None


def _chrome_exe() -> str | None:
    for p in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google" / "Chrome" / "Application" / "chrome.exe",
    ):
        if p.is_file():
            return str(p)
    return None


def ensure_browser_bridge(*, max_wait_sec: float = 30.0) -> None:
    env = _opencli_env()
    chrome = _chrome_exe()

    def _chrome_running() -> bool:
        try:
            r = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15, shell=False,
            )
            return "chrome.exe" in (r.stdout or "").lower()
        except Exception:
            return False

    extension_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "turnstilePatch"))
    
    if not _chrome_running() and chrome:
        print(f"[bridge] 启动可视化 Chrome 窗口(挂载 Turnstile 插件)...")
        cmd_str = f'start "" "{chrome}" --load-extension="{extension_dir}"'
        subprocess.Popen(
            cmd_str, shell=True, env=env,
        )
        time.sleep(4.0)

    deadline = time.time() + max_wait_sec
    restarted = False
    while time.time() < deadline:
        try:
            r = subprocess.run(
                [*OPENCLI, "doctor"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=20, shell=False, env=env,
            )
            out = (r.stdout or "") + "\n" + (r.stderr or "")
            if "[OK] Extension" in out or "Everything looks good" in out:
                return
        except Exception:
            pass

        if not restarted:
            print("[bridge] 扩展未连接，restart opencli daemon ...")
            try:
                subprocess.run(
                    [*OPENCLI, "daemon", "restart"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=30, shell=False, env=env,
                )
            except Exception as e:
                print(f"[bridge] daemon restart 失败: {e}", file=sys.stderr)
            restarted = True
            if not _chrome_running() and chrome:
                subprocess.Popen(
                    f'start "" "{chrome}" --load-extension="{extension_dir}"',
                    shell=True, env=env,
                )
            time.sleep(3.0)
            continue
        time.sleep(2.0)

    raise BridgeDisconnectedError(
        f"Browser Bridge 在 {max_wait_sec:.0f}s 内未能连接"
    )


# ─── JS 参数嵌入 ────────────────────────────────────────────────────────────────


def _embed_js(js_template: str, **kwargs) -> str:
    """将 Python 值以 JSON 字面量嵌入 JS 模板。占位符格式: %%name%%"""
    result = js_template
    for k, v in kwargs.items():
        result = result.replace(f"%%{k}%%", json.dumps(v, ensure_ascii=False))
    return result


# ─── 配置 ───────────────────────────────────────────────────────────────────────

_config: dict = {}


def load_config() -> dict:
    global _config
    if CONFIG_PATH.is_file():
        _config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        _config = {}
    return _config


def cfg(key: str, default=""):
    return _config.get(key, default)


# ─── HTTP 工具 ──────────────────────────────────────────────────────────────────


def _get_proxies() -> dict:
    proxy = str(cfg("proxy", "") or "").strip()
    return {"http": proxy, "https": proxy} if proxy else {}


def _get_user_agent() -> str:
    return cfg(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


def _is_proxy_error(exc) -> bool:
    return True


def http_get(url, **kwargs):
    import requests
    kw = dict(kwargs)
    proxies = kw.pop("proxies", None) or _get_proxies()
    timeout = kw.pop("timeout", 15)
    s = requests.Session()
    s.trust_env = False
    if proxies:
        try:
            return s.get(url, proxies=proxies, timeout=min(timeout, 2), **kw)
        except Exception:
            pass
    return s.get(url, timeout=timeout, **kw)


def http_post(url, **kwargs):
    import requests
    kw = dict(kwargs)
    proxies = kw.pop("proxies", None) or _get_proxies()
    timeout = kw.pop("timeout", 15)
    s = requests.Session()
    s.trust_env = False
    if proxies:
        try:
            return s.post(url, proxies=proxies, timeout=min(timeout, 2), **kw)
        except Exception:
            pass
    return s.post(url, timeout=timeout, **kw)


# ─── 邮箱服务 ───────────────────────────────────────────────────────────────────


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def normalize_mail_body(*sources):
    parts = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("text", "raw", "content", "intro", "body", "snippet"):
            value = source.get(key)
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                if isinstance(item, str) and item.strip():
                    parts.append(item)
        html_value = source.get("html")
        html_items = html_value if isinstance(html_value, (list, tuple)) else [html_value]
        for item in html_items:
            if isinstance(item, str) and item.strip():
                parts.append(re.sub(r"<[^>]+>", " ", item))
    return "\n".join(parts)


def extract_verification_code(text, subject=""):
    if subject:
        match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    for pattern in (
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "hydra:member", "data", "messages"):
            val = data.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict) and isinstance(val.get("messages"), list):
                return val["messages"]
    return []


# --- YYDS ---

def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or cfg("yyds_api_key")
    token = jwt or cfg("yyds_jwt")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def yyds_pick_domain(api_key=None, jwt=None):
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 没有返回任何可用域名")
    private = [d for d in domains if d.get("isVerified") and not d.get("isPublic")]
    if private:
        return private[0]["domain"]
    public = [d for d in domains if d.get("isVerified") and d.get("isPublic")]
    if public:
        return public[0]["domain"]
    verified = [d for d in domains if d.get("isVerified")]
    if verified:
        return verified[0]["domain"]
    raise Exception("YYDS 无已验证域名可用")


def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or cfg("yyds_api_key")
    token = jwt or cfg("yyds_jwt")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 创建邮箱失败: {data}")


def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or cfg("yyds_api_key")
    token = jwt or cfg("yyds_jwt")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 获取token失败: {data}")


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or cfg("yyds_api_key")
    token = jwt or cfg("yyds_jwt")
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = generate_username(10)
    result = yyds_create_account(address=username, domain=domain, api_key=key, jwt=token)
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("获取 YYDS token 失败")
    return address, temp_token


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or cfg("yyds_api_key")
    temp_token = token or jwt or cfg("yyds_jwt")
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages", params={"address": address}, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or cfg("yyds_api_key")
    temp_token = token or jwt or cfg("yyds_jwt")
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 获取邮件详情失败: {data}")


def yyds_get_oai_code(token, address, timeout=180, poll_interval=3, log=None):
    log = log or print
    deadline = time.time() + timeout
    seen_attempts = {}
    while time.time() < deadline:
        try:
            messages = yyds_get_messages(address, token=token)
        except Exception as exc:
            log(f"[Debug] YYDS 拉取邮件列表失败: {exc}")
            time.sleep(poll_interval)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id:
                continue
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            try:
                detail = yyds_get_message_detail(msg_id, token=token)
            except Exception as exc:
                log(f"[Debug] YYDS 获取邮件详情失败: {exc}")
                continue
            combined = normalize_mail_body(detail)
            subject = detail.get("subject", "")
            log(f"[Debug] YYDS 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                log(f"[*] YYDS 验证码: {code}")
                return code
        time.sleep(poll_interval)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


# --- DuckMail ---

def duckmail_get_domains(api_key=None):
    headers = {}
    key = api_key or cfg("duckmail_api_key")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def duckmail_create_account(address, password, api_key=None):
    headers = {"Content-Type": "application/json"}
    key = api_key or cfg("duckmail_api_key")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_post(
        f"{DUCKMAIL_API_BASE}/accounts",
        json={"address": address, "password": password, "expiresIn": 0},
        headers=headers,
    )
    resp.raise_for_status()
    return resp.json()


def duckmail_get_token(address, password):
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json={"address": address, "password": password})
    resp.raise_for_status()
    return resp.json().get("token")


def duckmail_get_email_and_token(api_key=None):
    key = api_key or cfg("duckmail_api_key")
    domains = duckmail_get_domains(api_key=key)
    if not domains:
        raise Exception("DuckMail 无可用域名")
    domain = domains[0].get("domain") if isinstance(domains[0], dict) else domains[0]
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    duckmail_create_account(address, password, api_key=key)
    token = duckmail_get_token(address, password)
    if not token:
        raise Exception("获取 DuckMail token 失败")
    return address, token


def duckmail_get_messages(token):
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def duckmail_get_message_detail(token, message_id):
    resp = http_get(
        f"{DUCKMAIL_API_BASE}/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()


def duckmail_get_oai_code(dev_token, email, timeout=180, poll_interval=3, log=None):
    log = log or print
    deadline = time.time() + timeout
    seen_attempts = {}
    while time.time() < deadline:
        try:
            messages = duckmail_get_messages(dev_token)
        except Exception as exc:
            log(f"[Debug] DuckMail 拉取邮件失败: {exc}")
            time.sleep(poll_interval)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if email.lower() not in recipients:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            try:
                detail = duckmail_get_message_detail(dev_token, msg_id)
            except Exception as exc:
                log(f"[Debug] DuckMail 获取详情失败: {exc}")
                continue
            combined = normalize_mail_body(detail)
            subject = detail.get("subject", "")
            log(f"[Debug] DuckMail 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                log(f"[*] DuckMail 验证码: {code}")
                return code
        time.sleep(poll_interval)
    raise Exception(f"DuckMail 在 {timeout}s 内未收到验证码邮件")


# --- Cloudflare ---

def _cf_api_base():
    return str(cfg("cloudflare_api_base", "") or "").rstrip("/")


def _cf_api_key():
    return cfg("cloudflare_api_key", "")


def _cf_auth_mode():
    return str(cfg("cloudflare_auth_mode", "none") or "none").lower()


def _cf_path(key, default):
    raw = str(cfg(key, default) or default).strip()
    return raw if raw.startswith("/") else "/" + raw


def _cf_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = _cf_api_key()
    mode = _cf_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def _cf_auth_params(params=None):
    merged = dict(params or {})
    key = _cf_api_key()
    if key and _cf_auth_mode() == "query-key":
        merged["key"] = key
    return merged


_cf_domain_index = 0


def _cf_next_domain():
    global _cf_domain_index
    domains = [x.strip() for x in str(cfg("defaultDomains", "") or "").split(",") if x.strip()]
    if not domains:
        return ""
    d = domains[_cf_domain_index % len(domains)]
    _cf_domain_index += 1
    return d


def cloudflare_get_email_and_token():
    api_base = _cf_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    path = _cf_path("cloudflare_path_accounts", "/api/new_address")
    url = f"{api_base}{path}"
    domain = _cf_next_domain()
    is_admin = path.rstrip("/").lower() == "/admin/new_address"
    if is_admin:
        payload = {"name": generate_username(10), "enablePrefix": True}
        if domain:
            payload["domain"] = domain
        headers = _cf_headers(content_type=True)
    else:
        payload = {}
        if domain:
            payload["domain"] = domain
        headers = {"Content-Type": "application/json"}
    resp = http_post(url, json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare 创建邮箱失败: {data}")
    return address, jwt


def cloudflare_get_messages(token):
    api_base = _cf_api_base()
    path = _cf_path("cloudflare_path_messages", "/messages")
    params = _cf_auth_params({"limit": 20, "offset": 0})
    resp = http_get(
        f"{api_base}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    resp.raise_for_status()
    return _pick_list_payload(resp.json())


def cloudflare_get_message_detail(token, message_id):
    api_base = _cf_api_base()
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{_cf_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(url, headers={"Authorization": f"Bearer {token}"}, params=_cf_auth_params())
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")


def cloudflare_get_oai_code(dev_token, email, timeout=180, poll_interval=3, log=None):
    log = log or print
    api_base = _cf_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    seen_attempts = {}
    while time.time() < deadline:
        try:
            messages = cloudflare_get_messages(dev_token)
        except Exception as exc:
            log(f"[Debug] Cloudflare 拉取邮件失败: {exc}")
            time.sleep(poll_interval)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            combined = normalize_mail_body(msg)
            subject = str(msg.get("subject", "") or "")
            try:
                detail = cloudflare_get_message_detail(dev_token, msg_id)
                detail_body = normalize_mail_body(detail)
                if detail_body:
                    combined += "\n" + detail_body
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception:
                pass
            log(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                log(f"[*] Cloudflare 验证码: {code}")
                return code
        time.sleep(poll_interval)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")


# --- CloudMail ---

_cloudmail_domain_index = 0


def cloudmail_get_email_and_token():
    api_base = str(cfg("cloudmail_api_base", "") or "").strip().rstrip("/")
    public_token = str(cfg("cloudmail_public_token", "") or "").strip()
    if not api_base:
        raise Exception("Cloud Mail API Base 未配置")
    if not public_token:
        raise Exception("Cloud Mail Public Token 未配置")
    domains = [
        item.strip().lstrip("@")
        for item in str(cfg("cloudmail_domains", "") or "").split(",")
        if item.strip().lstrip("@")
    ]
    if not domains:
        raise Exception("Cloud Mail 收件域名未配置")
    global _cloudmail_domain_index
    domain = domains[_cloudmail_domain_index % len(domains)]
    _cloudmail_domain_index += 1
    address = f"{generate_username(12)}@{domain}"
    return address, f"cloudmail:{address}"


def cloudmail_get_messages(address):
    api_base = str(cfg("cloudmail_api_base", "") or "").strip().rstrip("/")
    public_token = str(cfg("cloudmail_public_token", "") or "").strip()
    path = str(cfg("cloudmail_path_messages", "/api/public/emailList") or "/api/public/emailList").strip()
    if not path.startswith("/"):
        path = "/" + path
    resp = http_post(
        f"{api_base}{path}",
        headers={"Authorization": public_token, "Content-Type": "application/json"},
        json={"toEmail": address, "type": 0, "isDel": 0, "timeSort": "desc", "num": 1, "size": 20},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise Exception(f"Cloud Mail 返回格式错误: {data}")
    code = data.get("code")
    if code not in (None, 200, "200"):
        raise Exception(f"Cloud Mail 失败: code={code}, message={data.get('message', '')}")
    messages = data.get("data")
    if isinstance(messages, list):
        return messages
    return _pick_list_payload(data)


def cloudmail_get_oai_code(dev_token, email, timeout=180, poll_interval=3, log=None):
    log = log or print
    deadline = time.time() + timeout
    seen_attempts = {}
    while time.time() < deadline:
        try:
            messages = cloudmail_get_messages(email)
        except Exception as exc:
            log(f"[Debug] Cloud Mail 拉取邮件失败: {exc}")
            time.sleep(poll_interval)
            continue
        for msg in messages:
            msg_id = msg.get("emailId") or msg.get("email_id") or msg.get("id")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            target = str(msg.get("toEmail") or msg.get("to_email") or "").strip().lower()
            if target and target != email.lower():
                continue
            code_value = str(msg.get("code", "") or "").strip()
            combined = normalize_mail_body(msg)
            if code_value:
                combined = f"verification code: {code_value}\n{combined}"
            subject = str(msg.get("subject", "") or "")
            log(f"[Debug] Cloud Mail 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                log(f"[*] Cloud Mail 验证码: {code}")
                return code
        time.sleep(poll_interval)
    raise Exception(f"Cloud Mail 在 {timeout}s 内未收到验证码邮件")


# --- Provider 分发 ---

def get_email_and_token(provider=None):
    provider = provider or cfg("email_provider", "duckmail")
    if provider == "yyds":
        return yyds_get_email_and_token()
    if provider == "cloudmail":
        return cloudmail_get_email_and_token()
    if provider == "cloudflare":
        return cloudflare_get_email_and_token()
    return duckmail_get_email_and_token()


def get_oai_code(dev_token, email, provider=None, timeout=180, poll_interval=3, log=None):
    provider = provider or cfg("email_provider", "duckmail")
    if provider == "yyds":
        return yyds_get_oai_code(dev_token, email, timeout=timeout, poll_interval=poll_interval, log=log)
    if provider == "cloudmail":
        return cloudmail_get_oai_code(dev_token, email, timeout=timeout, poll_interval=poll_interval, log=log)
    if provider == "cloudflare":
        return cloudflare_get_oai_code(dev_token, email, timeout=timeout, poll_interval=poll_interval, log=log)
    return duckmail_get_oai_code(dev_token, email, timeout=timeout, poll_interval=poll_interval, log=log)


# ─── 注册步骤 ───────────────────────────────────────────────────────────────────


def get_current_url() -> str:
    return eval_js("(()=>location.href)()") or ""


def wait_doc_loaded(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = eval_js("(()=>document.readyState)()")
        if "complete" in (r or ""):
            return
        time.sleep(0.5)
    raise RuntimeError("页面加载超时")


def _clear_js_cookies_and_storage():
    """清除当前域下所有 JS 可访问的 cookie、localStorage、sessionStorage。"""
    eval_js(r"""(() => {
        try {
            const expiry = 'expires=Thu, 01 Jan 1970 00:00:00 GMT';
            const domains = ['', location.hostname,
                             '.grok.com', '.x.ai', '.accounts.x.ai'];
            document.cookie.split(';').forEach(c => {
                const name = c.split('=')[0].trim();
                if (!name) return;
                domains.forEach(d => {
                    const dm = d ? ';domain=' + d : '';
                    document.cookie = name + '=;' + expiry + ';path=/' + dm;
                });
            });
            localStorage.clear();
            sessionStorage.clear();
        } catch(e) {}
    })()""")


def clear_all_browser_cookies(log=print):
    """清除浏览器中 grok.com / accounts.x.ai 的所有 Cookie。

    关键步骤：
      1. 导航到 grok.com，用同步 XHR 调用服务端 sign-out 接口
         （服务端会用 Set-Cookie 使 HttpOnly 的 sso / sso-rw 过期）。
      2. 清除 grok.com 上 JS 可访问的 cookie 和 storage。
      3. 导航到 accounts.x.ai/logout，服务端清除 auth session。
      4. 清除 accounts.x.ai 上 JS 可访问的 cookie 和 storage。
    """
    log("[*] [前置动作] 正在清除 Cookie 与 Session（含 HttpOnly）...")

    # ── Step 1: 导航到 grok.com ──────────────────────────────────────────────
    try:
        eval_js(r"(() => { location.href = 'https://grok.com'; })()")
        time.sleep(2)
        wait_doc_loaded(timeout=10)
    except Exception:
        pass

    # ── Step 2: 在 grok.com 上调用服务端 sign-out（清 HttpOnly cookie）─────
    try:
        result = eval_js(r"""(() => {
            const results = [];
            const endpoints = [
                '/rest/auth/sign-out',
                '/rest/auth/logout',
                '/api/auth/sign-out'
            ];
            for (const ep of endpoints) {
                try {
                    const xhr = new XMLHttpRequest();
                    xhr.open('POST', ep, false);      // 同步
                    xhr.withCredentials = true;
                    xhr.setRequestHeader('Content-Type', 'application/json');
                    xhr.send('{}');
                    results.push(ep + ':' + xhr.status);
                    if (xhr.status >= 200 && xhr.status < 400) break;
                } catch(e) {
                    results.push(ep + ':err');
                }
            }
            return results.join(' | ');
        })()""", timeout=15)
        log(f"[*] [前置动作] grok.com sign-out: {result}")
    except Exception as e:
        log(f"[!] [前置动作] grok.com sign-out 异常: {e}")

    # ── Step 3: 清除 grok.com 上 JS 可访问的 cookie / storage ───────────────
    try:
        _clear_js_cookies_and_storage()
    except Exception:
        pass

    # ── Step 4: 导航到 accounts.x.ai/logout ──────────────────────────────────
    try:
        eval_js(r"(() => { location.href = 'https://accounts.x.ai/logout'; })()")
        time.sleep(3)
        wait_doc_loaded(timeout=10)
    except Exception:
        pass

    # ── Step 5: 清除 accounts.x.ai 上 JS 可访问的 cookie / storage ──────────
    try:
        _clear_js_cookies_and_storage()
    except Exception:
        pass

    log("[*] [前置动作] Cookie 及 Session 清除完成")


def open_signup_page(log=print):
    clear_all_browser_cookies(log=log)

    log("[*] 正在打开注册页面(拉至前台)...")
    run("open", SIGNUP_URL, "--window", "foreground", timeout=30)
    time.sleep(3)
    wait_doc_loaded(timeout=20)

    # 等待落地在 accounts.x.ai，最多重试 3 次
    for attempt in range(1, 4):
        curr = get_current_url()
        log(f"[*] 当前URL: {curr}")
        if "accounts.x.ai" in curr.lower():
            break
        log(f"[*] 仍未到达 accounts.x.ai (第{attempt}次)，重试...")
        # 每次重试前再清一遍 cookie
        clear_all_browser_cookies(log=log)
        run("open", SIGNUP_URL, "--window", "foreground", timeout=30)
        time.sleep(3)
        wait_doc_loaded(timeout=15)
    else:
        raise Exception(
            f"无法到达 accounts.x.ai 注册页，当前 URL: {get_current_url()}"
        )

    click_email_signup_button(log=log)


def click_email_signup_button(timeout=20, log=print):
    deadline = time.time() + timeout
    while time.time() < deadline:
        curr = get_current_url()
        if "accounts.x.ai" not in curr.lower():
            time.sleep(1)
            continue

        has_email = eval_js(r"""(()=>{
            const el = document.querySelector('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]');
            return !!el;
        })()""")
        if has_email:
            log("[*] 已处于邮箱填写页面，无需点击「使用邮箱注册」")
            return True

        clicked = eval_js(r"""(()=>{
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [node.innerText, node.textContent, node.getAttribute('aria-label'),
            node.getAttribute('title'), node.getAttribute('href')]
        .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter(n => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true')
    .map(n => ({ node: n, score: scoreEntry(n), text: nodeText(n) }))
    .filter(item => item.score > 0)
    .sort((a, b) => b.score - a.score);
if (!candidates.length) return '';
candidates[0].node.click();
return candidates[0].text || 'clicked';
})()""")
        if clicked:
            log(f"[*] 已点击「使用邮箱注册」: {clicked}")
            time.sleep(2)
            return True
        time.sleep(1)
    raise Exception("未找到「使用邮箱注册」按钮")


def fill_email_and_submit(email, timeout=45, log=print):
    deadline = time.time() + timeout
    last_reclick = 0.0
    while time.time() < deadline:
        js = _embed_js(r"""(()=>{
const email = %%email%%;
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [node.innerText, node.textContent, node.getAttribute('aria-label'),
            node.getAttribute('placeholder'), node.getAttribute('data-testid'),
            node.getAttribute('name'), node.getAttribute('id'), node.getAttribute('autocomplete')]
        .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll(
        'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden','submit','button','checkbox','radio','file','search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('mail') || meta.includes('邮箱')) direct.push(node);
    }
    return Array.from(new Set(direct));
}
const input = emailCandidates().find(n => isVisible(n) && !n.disabled && !n.readOnly) || null;
if (!input) return JSON.stringify({state:'not-ready', url:location.href});
input.focus(); input.click();
const valueProto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const valueSetter = Object.getOwnPropertyDescriptor(valueProto, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, data:email, inputType:'insertText'}));
input.dispatchEvent(new InputEvent('input', {bubbles:true, data:email, inputType:'insertText'}));
input.dispatchEvent(new Event('change', {bubbles:true}));
const inputType = (input.getAttribute('type') || '').toLowerCase();
const isValid = inputType !== 'email' || input.checkValidity();
if ((input.value || '').trim() !== email || !isValid) return JSON.stringify({state:'fill-failed'});
input.blur();
return JSON.stringify({state:'filled'});
})()""", email=email)
        raw = eval_js(js)
        filled = extract_json(raw)
        state = filled.get("state") if isinstance(filled, dict) else raw

        if state == "not-ready":
            now = time.time()
            if now - last_reclick >= 3:
                eval_js(r"""(()=>{
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter(n => { const s = window.getComputedStyle(n); return s.display !== 'none' && n.getBoundingClientRect().width > 0; })
    .filter(n => { const t = (n.textContent||'').replace(/\s+/g,'').toLowerCase(); return t.includes('使用邮箱注册') || t.includes('signupwithemail') || t.includes('continuewithemail'); });
if (candidates.length) candidates[0].click();
return candidates.length ? 'clicked' : '';
})()""")
                last_reclick = now
            time.sleep(0.5)
            continue

        if state != "filled":
            time.sleep(0.5)
            continue

        time.sleep(0.8)
        clicked = eval_js(r"""(()=>{
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [node.innerText, node.textContent, node.getAttribute('aria-label'),
            node.getAttribute('placeholder'), node.getAttribute('data-testid'),
            node.getAttribute('name'), node.getAttribute('id'), node.getAttribute('autocomplete')]
        .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll(
        'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]'));
    return direct;
}
const input = emailCandidates().find(n => isVisible(n) && !n.disabled && !n.readOnly) || null;
if (!input || !(input.value || '').trim()) return '';
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter(n => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find(node => {
    const text = textOf(node).replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return text === '注册' || text.includes('注册') || text.includes('继续') || text.includes('下一步')
        || lower.includes('signup') || lower.includes('continue') || lower.includes('next') || lower.includes('submit');
});
if (submitButton) { submitButton.click(); return textOf(submitButton) || 'clicked'; }
const form = input.closest('form');
if (form) { if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event('submit', {bubbles:true, cancelable:true})); return 'form-submit'; }
input.focus();
input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', bubbles:true, cancelable:true}));
return 'enter';
})()""")
        if clicked:
            log(f"[*] 已填写邮箱并提交: {email} ({clicked})")
            return True
        time.sleep(0.5)

    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(code, timeout=60, log=print):
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout
    while time.time() < deadline:
        js = _embed_js(r"""(()=>{
const code = String(%%code%% || '').trim();
if (!code) return 'empty-code';
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value); else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, data:value, inputType:'insertText'}));
    input.dispatchEvent(new InputEvent('input', {bubbles:true, data:value, inputType:'insertText'}));
    input.dispatchEvent(new Event('change', {bubbles:true}));
}
const aggregate = Array.from(document.querySelectorAll(
    'input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]'
)).find(n => isVisible(n) && !n.disabled && !n.readOnly && Number(n.maxLength || 6) > 1);
if (aggregate) {
    aggregate.focus(); aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}
const otpBoxes = Array.from(document.querySelectorAll('input')).filter(n => {
    if (!isVisible(n) || n.disabled || n.readOnly) return false;
    const maxLength = Number(n.maxLength || 0);
    const ac = String(n.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});
if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i++) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus(); box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, key:ch}));
        box.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, key:ch}));
    }
    const merged = otpBoxes.slice(0, code.length).map(x => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}
return 'not-ready';
})()""", code=clean_code)
        filled = eval_js(js)

        if filled == "not-ready":
            time.sleep(0.5)
            continue
        if "failed" in str(filled):
            log(f"[Debug] 验证码填写失败: {filled}")
            time.sleep(0.5)
            continue

        clicked = eval_js(r"""(()=>{
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'))
    .filter(n => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true');
const btn = buttons.find(node => {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return t.includes('确认邮箱') || t.includes('继续') || t.includes('下一步')
        || t.includes('confirm') || t.includes('continue') || t.includes('next');
});
if (!btn) return 'no-button';
btn.focus(); btn.click();
return 'clicked';
})()""")
        if clicked in ("clicked", "no-button"):
            log(f"[*] 已填写验证码并提交: {code}")
            time.sleep(1.5)
            return True
        time.sleep(0.5)

    raise Exception("验证码填写/提交失败")


def get_turnstile_token(timeout=20, log=print):
    eval_js("(()=>{ try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {} return 'ok'; })()")
    for _ in range(timeout):
        token = eval_js(r"""(()=>{
try {
    const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
    if (byInput) return byInput;
    if (window.turnstile && typeof turnstile.getResponse === 'function')
        return String(turnstile.getResponse() || '').trim();
    return '';
} catch(e) { return ''; }
})()""")
        if len(token or "") >= 80:
            log(f"[*] Turnstile 已通过，token长度={len(token)}")
            return token
        eval_js(r"""(()=>{
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter(n => {
    const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
    return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
return 'attempted';
})()""")
        time.sleep(1)
    raise Exception("Turnstile 获取 token 失败")


def _kick_turnstile(log=print, reset=False):
    # widget 渲染在 input[name=cf-turnstile-response] 父 div 的 closed shadow root 里，
    # JS 看不到内部（实测无 iframe 暴露）；CDP 原生点击落在复选框坐标
    # （父 div 左 20px、垂直居中）即可勾选通过。reset 只在首次执行。
    if reset:
        try:
            eval_js("(()=>{ try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {} try { if (window.turnstile && typeof turnstile.execute === 'function') turnstile.execute(); } catch(e) {} return 'ok'; })()")
            time.sleep(2)
        except Exception:
            pass
    rect = eval_js(r"""(()=>{
        const inp = document.querySelector('input[name="cf-turnstile-response"]');
        if (!inp || !inp.parentElement) return 'no-host';
        const host = inp.parentElement;
        const r = host.getBoundingClientRect();
        const s = getComputedStyle(host);
        if (r.width < 50 || r.height < 30 || s.display === 'none' || s.visibility === 'hidden')
            return 'hidden:' + Math.round(r.width) + 'x' + Math.round(r.height);
        let a = document.getElementById('__cf_click_anchor');
        if (!a) {
            a = document.createElement('div');
            a.id = '__cf_click_anchor';
            a.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;background:transparent;width:20px;height:20px;';
            document.body.appendChild(a);
        }
        a.style.left = (r.left + 10) + 'px';
        a.style.top = (r.top + r.height / 2 - 10) + 'px';
        return 'ok:' + r.left.toFixed(1) + ',' + r.top.toFixed(1) + ',' + r.width.toFixed(1) + ',' + r.height.toFixed(1);
    })()""")
    log(f"[Debug] turnstile widget host: {rect}")
    if not str(rect).startswith("ok:"):
        return False
    try:
        out = run("click", "#__cf_click_anchor", timeout=10)
        log(f"[Debug] turnstile click: {out[:150]}")
        return True
    except Exception as e:
        log(f"[Debug] turnstile click 失败: {e}")
        return False


def _dump_turnstile_debug(log=print):
    try:
        ts = time.strftime("%H%M%S")
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"cf_debug_{ts}.png")
        run("screenshot", path, timeout=15)
        log(f"[Debug] 已保存截图: {path}")
    except Exception as e:
        log(f"[Debug] 截图失败: {e}")
    try:
        log(f"[Debug] frames: {run('frames', timeout=10)[:500]}")
    except Exception as e:
        log(f"[Debug] frames 获取失败: {e}")
    try:
        info = eval_js(r"""(()=>{
            const inp = document.querySelector('input[name="cf-turnstile-response"]');
            const state = inp
                ? {hasInput: true, tokenLen: String(inp.value || '').length, parentHasShadow: !!(inp.parentElement && inp.parentElement.shadowRoot)}
                : {hasInput: false};
            return JSON.stringify(state) + ' turnstile=' + (typeof window.turnstile) + ' url=' + location.href;
        })()""")
        log(f"[Debug] 页面状态: {info}")
    except Exception as e:
        log(f"[Debug] 页面状态获取失败: {e}")


def build_profile():
    given_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given = random.choice(given_pool)
    family = random.choice(family_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given, family, password


def fill_profile_and_submit(timeout=120, log=print):
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled = False
    wait_cf_since = None
    last_cf_kick = 0.0
    last_cf_log = 0.0
    cf_hint_shown = False
    cf_kicked = False

    while time.time() < deadline:
        if not form_filled:
            js = _embed_js(r"""(()=>{
const givenName = %%given_name%%;
const familyName = %%family_name%%;
const password = %%password%%;
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find(n => isVisible(n) && !n.disabled && !n.readOnly) || null;
}
function setInputValue(input, value) {
    if (!input) return false;
    input.focus(); input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value); else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, data:value, inputType:'insertText'}));
    input.dispatchEvent(new InputEvent('input', {bubbles:true, data:value, inputType:'insertText'}));
    input.dispatchEvent(new Event('change', {bubbles:true}));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}
const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');
if (!givenInput || !familyInput || !passwordInput) return 'not-ready';
const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);
if (!ok1 || !ok2 || !ok3) return 'fill-failed';
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    if (token.length < 80) return 'wait-cloudflare:' + token.length;
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter(n => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true');
const submitBtn = buttons.find(n => {
    const t = (n.innerText || n.textContent || '').replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (submitBtn) return 'ready-to-submit';
return 'filled-no-submit';
})()""", given_name=given_name, family_name=family_name, password=password)
            filled = eval_js(js)
            if filled in ("not-ready", "fill-failed"):
                time.sleep(0.8)
                continue
            form_filled = True

        # Turnstile 检测与 Token 等待
        cf_status = eval_js(r"""(()=>{
            const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
            const token = String((cfInput && cfInput.value) || '').trim();
            if (token.length >= 80) return 'solved:' + token;
            
            if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                try {
                    const res = String(window.turnstile.getResponse() || '').trim();
                    if (res.length >= 80) return 'solved:' + res;
                } catch(e) {}
            }

            const cfPresent = !!cfInput || !!document.querySelector('iframe[src*="turnstile"], iframe[src*="cloudflare"], div.cf-turnstile, [data-sitekey]');
            return cfPresent ? 'waiting:' + token.length : 'not-present';
        })()""")

        if isinstance(cf_status, str) and cf_status.startswith("solved:"):
            token = cf_status.split(":", 1)[1]
            _sync_turnstile_token(token)
            log(f"[*] Turnstile 验证已通过，Token 长度={len(token)}")
        elif isinstance(cf_status, str) and cf_status.startswith("waiting:"):
            token_len = cf_status.split(":", 1)[1]
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            cf_waited = now - wait_cf_since
            if now - last_cf_log >= 5:
                log(f"[*] 资料已填写，等待 Turnstile 验证... Token 长度={token_len} (已等 {cf_waited:.0f}s)")
                last_cf_log = now
            if now - last_cf_kick >= 6:
                first_kick = not cf_kicked
                cf_kicked = True
                last_cf_kick = now
                log("[*] 主动触发 Turnstile (CDP 原生点击复选框)...")
                _kick_turnstile(log=log, reset=first_kick)
            if cf_waited >= 45 and not cf_hint_shown:
                cf_hint_shown = True
                log("[!] [提示] Turnstile 45s 未通过，请确认 turnstilePatch 扩展已加载，或在前台 Chrome 手动点击复选框")
                _dump_turnstile_debug(log=log)
            time.sleep(1)
            continue

        # 点击提交按钮
        submit_state = eval_js(r"""(()=>{
            function isVisible(node) {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = node.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }
            function buttonText(node) {
                return [node.innerText, node.textContent, node.getAttribute('value'),
                        node.getAttribute('aria-label'), node.getAttribute('title')]
                    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
            }
            const passwordInput = document.querySelector('input[type="password"], input[data-testid="password"], input[name="password"]');
            if (!passwordInput) return 'form-gone';
            const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
                .filter(n => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true');
            let submitBtn = buttons.find(n => {
                const t = buttonText(n).replace(/\s+/g, '').toLowerCase();
                return t.includes('完成注册') || t.includes('创建账户') || t.includes('注册') || t.includes('signup') || t.includes('createaccount');
            });
            if (!submitBtn) submitBtn = buttons.find(n => (n.getAttribute('type') || '').toLowerCase() === 'submit');
            if (!submitBtn) return 'no-submit-button';
            submitBtn.focus(); submitBtn.click();
            return 'submitted';
        })()""")

        if submit_state == "submitted":
            log(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        if submit_state == "form-gone":
            log(f"[*] 注册表单已消失（视为已提交）: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}

        time.sleep(0.5)

    if wait_cf_since is not None:
        raise Exception(f"注册资料已填写，但 Turnstile 在 {timeout}s 内未通过")
    raise Exception("注册资料填写/提交失败")


def _sync_turnstile_token(token: str):
    js = _embed_js(r"""(()=>{
const token = String(%%token%% || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return '0';
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token); else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', {bubbles:true}));
cfInput.dispatchEvent(new Event('change', {bubbles:true}));
return String(cfInput.value || '').trim().length;
})()""", token=token)
    eval_js(js)


# ─── SSO 间接获取 ───────────────────────────────────────────────────────────────


def _try_document_cookie_sso() -> str | None:
    # 1. 先尝试通过 JS document.cookie 获取
    raw = eval_js(r"""(()=>{
const cookies = document.cookie.split(';');
for (const c of cookies) {
    const parts = c.trim().split('=');
    const name = parts[0];
    if (name === 'sso' || name === 'sso-rw') {
        return parts.slice(1).join('=');
    }
}
return '';
})()""")
    val = (raw or "").strip()
    if len(val) > 20:
        return val

    # 2. JS 无法读取 HttpOnly Cookie，直接调用 opencli 提取底座域 Cookies
    try:
        env = _opencli_env()
        r = subprocess.run(
            [*OPENCLI, "browser", SESSION, "cookies", "--url", "https://grok.com"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, env=env,
        )
        out = r.stdout or ""
        # 匹配 sso 或 sso-rw
        for line in out.splitlines():
            if "sso" in line.lower():
                parts = line.strip().split()
                for p in parts:
                    if len(p) > 50 and not p.startswith("http") and not p.startswith("grok"):
                        return p
        # 也可通过 JSON 解析输出
        try:
            items = json.loads(out)
            if isinstance(items, list):
                for item in items:
                    name = item.get("name", "")
                    if name in ("sso", "sso-rw"):
                        v = item.get("value", "")
                        if len(v) > 20:
                            return v
        except Exception:
            pass
    except Exception:
        pass

    return None


def _try_storage_sso() -> str | None:
    raw = eval_js(r"""(()=>{
for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    const kl = key.toLowerCase();
    if (kl.includes('sso') || kl.includes('token') || kl.includes('auth')) {
        const val = localStorage.getItem(key);
        if (val && val.length > 50) return val;
    }
}
for (let i = 0; i < sessionStorage.length; i++) {
    const key = sessionStorage.key(i);
    const kl = key.toLowerCase();
    if (kl.includes('sso') || kl.includes('token') || kl.includes('auth')) {
        const val = sessionStorage.getItem(key);
        if (val && val.length > 50) return val;
    }
}
return '';
})()""")
    val = (raw or "").strip()
    return val if len(val) > 50 else None


def _try_auth_fetch_sso() -> str | None:
    raw = eval_js(r"""(()=>{
return fetch('https://grok.com/rest/auth/get-user', {credentials:'include'})
    .then(r => r.text())
    .then(t => t)
    .catch(e => '');
})()""", timeout=15)
    if raw and len(raw) > 10:
        data = extract_json(raw)
        if isinstance(data, dict):
            for key in ("sso", "token", "accessToken", "ssoToken"):
                val = data.get(key)
                if val and len(str(val)) > 50:
                    return str(val)
    return None


def _retry_submit_on_final_page(log=print):
    eval_js(r"""(()=>{
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find(el => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    const lower = t.toLowerCase();
    return t.includes('完成注册') || lower.includes('completeyoursignup') || lower.includes('completesignup');
});
if (!titleHit) return 'not-final-page';
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    if (token.length < 80) return 'wait-cf';
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"]'))
    .filter(n => isVisible(n) && !n.disabled);
const submitBtn = buttons.find(n => {
    const t = (n.innerText || n.textContent || '').replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (submitBtn) { submitBtn.focus(); submitBtn.click(); return 'clicked'; }
return 'no-btn';
})()""")


def discover_oidc_endpoints(log=print) -> dict:
    """动态获取并校验 OIDC 配置 Endpoint (auth.x.ai)"""
    import urllib.request
    import json
    disc_url = "https://auth.x.ai/.well-known/openid-configuration"
    try:
        req = urllib.request.Request(
            disc_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "grok-register-cpa/1.0",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            dev_ep = data.get("device_authorization_endpoint")
            tok_ep = data.get("token_endpoint")
            if dev_ep and tok_ep:
                log(f"[*] OIDC 服务发现成功: device_ep={dev_ep}, token_ep={tok_ep}")
                return {"device_authorization_endpoint": dev_ep, "token_endpoint": tok_ep}
    except Exception as e:
        log(f"[!] OIDC 服务发现异常 ({e})，将降级使用静态 Endpoint")
    return {
        "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }


def request_device_code(device_endpoint: str = None) -> dict:
    import urllib.request
    import urllib.parse
    import json
    url = device_endpoint or "https://auth.x.ai/oauth2/device/code"
    payload = urllib.parse.urlencode({
        "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
        "scope": "openid profile email offline_access grok-cli:access api:access",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "grok-register-cpa/1.0",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def poll_device_token(device_code: str, interval: int = 5, timeout: int = 120, token_endpoint: str = None, log=print) -> dict:
    import urllib.request
    import urllib.parse
    import json
    url = token_endpoint or "https://auth.x.ai/oauth2/token"
    deadline = time.time() + timeout
    payload = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code.strip(),
        "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
    }).encode("utf-8")

    curr_interval = interval
    net_streak = 0

    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": "grok-register-cpa/1.0",
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if "access_token" in data:
                    return data
        except urllib.error.HTTPError as e:
            net_streak = 0
            try:
                err_body = json.loads(e.read().decode("utf-8"))
                err = err_body.get("error", "")
                if err == "authorization_pending":
                    time.sleep(curr_interval)
                    continue
                elif err == "slow_down":
                    curr_interval = min(curr_interval + 5, 30)
                    log(f"[*] 触发 slow_down 调整轮询间隔为 {curr_interval}s")
                    time.sleep(curr_interval)
                    continue
                elif err in ("expired_token", "access_denied"):
                    log(f"[!] 设备码已失效或拒绝授权: {err}")
                    return {"error": err}
                elif err == "invalid_grant":
                    log(f"[!] 无效的授权(invalid_grant): {err_body}")
                    return {"error": "invalid_grant", "details": err_body}
            except Exception:
                pass
            time.sleep(curr_interval)
        except Exception as e:
            net_streak += 1
            if net_streak <= 20:
                time.sleep(curr_interval)
            else:
                log(f"[!] 轮询遭遇持续网络故障: {e}")
                break

    return {}


def save_cpa_json_credential(token_result: dict, token_endpoint: str = None, auth_dir: str = "cpa_auths", hotload_dir: str = None, log=print) -> str:
    """提取 Token、解析 JWT 生成标准 CPA xAI JSON 并落盘与支持热加载保存"""
    import base64
    import json
    from datetime import datetime, timezone

    access_token = token_result.get("access_token", "")
    refresh_token = token_result.get("refresh_token", "")
    id_token = token_result.get("id_token", "")

    if not access_token:
        return ""

    email = ""
    sub = ""

    # 解析 JWT (优先从 id_token 提，其次从 access_token)
    for jwt in (id_token, access_token):
        if not jwt or jwt.count(".") < 2:
            continue
        try:
            part = jwt.split(".")[1]
            rem = len(part) % 4
            if rem > 0:
                part += "=" * (4 - rem)
            decoded = json.loads(base64.urlsafe_b64decode(part).decode("utf-8"))
            email = email or decoded.get("email", "")
            sub = sub or decoded.get("sub", "")
            if email and sub:
                break
        except Exception:
            pass

    now = datetime.now(timezone.utc)
    expires_in = token_result.get("expires_in", 21600)
    exp_time = datetime.fromtimestamp(now.timestamp() + expires_in, tz=timezone.utc)

    cpa_payload = {
        "type": "xai",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": token_result.get("token_type", "Bearer"),
        "expires_in": expires_in,
        "expired": exp_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_refresh": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "email": email,
        "sub": sub,
        "base_url": "https://cli-chat-proxy.grok.com/v1",
        "redirect_uri": "http://127.0.0.1:56121/callback",
        "token_endpoint": token_endpoint or "https://auth.x.ai/oauth2/token",
        "auth_kind": "oauth"
    }

    # 原子落盘
    os.makedirs(auth_dir, exist_ok=True)
    filename = f"xai-{email or sub or 'token'}.json"
    file_path = os.path.join(auth_dir, filename)

    try:
        temp_path = file_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(cpa_payload, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(temp_path, 0o600)
        except Exception:
            pass
        os.replace(temp_path, file_path)
        log(f"[*] CPA 凭证成功保存至: {file_path}")

        # 若配置了热加载目录，同步复制
        if hotload_dir:
            os.makedirs(hotload_dir, exist_ok=True)
            hot_path = os.path.join(hotload_dir, filename)
            shutil.copy2(file_path, hot_path)
            log(f"[*] CPA 凭证成功热加载至: {hot_path}")

        return file_path
    except Exception as e:
        log(f"[!] 保存 CPA 凭证失败: {e}")
        return ""


def open_signin_and_login_with_email(email: str, password: str, log=print, timeout=60):
    log("[*] 正在拉起独立 Chromium (DrissionPage) 访问 sign-in...")
    try:
        from DrissionPage import ChromiumOptions, Chromium
    except ImportError:
        log("[!] 未安装 DrissionPage，请先 pip install DrissionPage")
        return

    try:
        co = ChromiumOptions()
        co.auto_port()
        extension_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "turnstilePatch"))
        if os.path.isdir(extension_dir):
            co.add_extension(extension_dir)

        browser = Chromium(co)
        tab = browser.latest_tab
        tab.get("https://accounts.x.ai/sign-in")

        deadline = time.time() + timeout
        log("[*] 寻找【使用邮箱登录】按钮并自动完成登录...")
        
        while time.time() < deadline:
            try:
                curr_url = tab.url
                if "grok.com" in curr_url or ("/account" in curr_url and "sign-in" not in curr_url):
                    log(f"[*] 登录成功/已跳转: {curr_url}")
                    break
            except Exception:
                pass

            try:
                # 阶段一：选择邮箱登录
                email_btn = tab.ele('text:使用邮箱登录', timeout=0.5) or tab.ele('text:Continue with email', timeout=0.5) or tab.ele('text:Sign in with email', timeout=0.5)
                if email_btn:
                    log("[*] 找到【使用邮箱登录】按钮，执行点击...")
                    email_btn.click()
                    time.sleep(2.0)
                    continue

                # 阶段二/三：查找输入框
                email_input = tab.ele('css:input[type="email"], input[name="email"], input[autocomplete="email"]', timeout=0.5)
                password_input = tab.ele('css:input[type="password"], input[name="password"]', timeout=0.5)

                if email_input and not password_input:
                    tab.run_js(f"""
                        const input = document.querySelector('input[type="email"], input[name="email"], input[autocomplete="email"]');
                        if (input && !input.value) {{
                            const valueSetter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), 'value')?.set;
                            if (valueSetter) valueSetter.call(input, '{email}');
                            else input.value = '{email}';
                            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                    """)
                    time.sleep(0.5)
                    next_btn = tab.ele('text:下一步', timeout=0.5) or tab.ele('text:Next', timeout=0.5) or tab.ele('text:Continue', timeout=0.5) or tab.ele('text:继续', timeout=0.5)
                    if next_btn:
                        log("[*] 找到【下一步】按钮，执行点击...")
                        next_btn.click()
                        time.sleep(2.0)
                    continue

                if email_input and password_input:
                    tab.run_js(f"""
                        const emailInput = document.querySelector('input[type="email"], input[name="email"], input[autocomplete="email"]');
                        if (emailInput && !emailInput.value) {{
                            const valueSetter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(emailInput), 'value')?.set;
                            if (valueSetter) valueSetter.call(emailInput, '{email}');
                            else emailInput.value = '{email}';
                            emailInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            emailInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                        const pwdInput = document.querySelector('input[type="password"], input[name="password"]');
                        if (pwdInput && !pwdInput.value) {{
                            const valueSetter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(pwdInput), 'value')?.set;
                            if (valueSetter) valueSetter.call(pwdInput, '{password}');
                            else pwdInput.value = '{password}';
                            pwdInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            pwdInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                    """)
                    time.sleep(0.5)
                    login_btn = tab.ele('text:登录', timeout=0.5) or tab.ele('text:Sign in', timeout=0.5) or tab.ele('text:Log in', timeout=0.5)
                    if login_btn:
                        log("[*] 找到【登录】按钮，执行点击...")
                        login_btn.click()
                        time.sleep(1.5)
                        
                        log("[*] 尝试主动触发 Turnstile (点击复选框)...")
                        try:
                            cf_box = tab.ele('css:#__cf_click_anchor', timeout=2)
                            if cf_box:
                                cf_box.click()
                        except:
                            pass
                        time.sleep(4.0)
                    continue

            except Exception as loop_e:
                pass
                
            time.sleep(1.5)

        log("[*] Chromium 自动化流程结束，正在关闭浏览器...")
        browser.quit()

    except Exception as e:
        log(f"[!] Chromium 自动化异常: {e}")
        try:
            browser.quit()
        except:
            pass


def authorize_grok_build(email: str, password: str, log=print, timeout=120) -> str:
    log("[*] 1. 动态获取 OIDC Endpoint 配置...")
    endpoints = discover_oidc_endpoints(log=log)
    device_ep = endpoints.get("device_authorization_endpoint")
    token_ep = endpoints.get("token_endpoint")

    log("[*] 等待资料提交后登录跳转完成 (到达 grok.com)...")
    wait_deadline = time.time() + 30
    while time.time() < wait_deadline:
        curr = get_current_url()
        if "grok.com" in curr:
            log("[*] 登录跳转完成，当前处于 grok.com")
            break
        time.sleep(1)
    else:
        log("[!] 提示：未在 30 秒内检测到跳转至 grok.com，直接尝试授权导航...")

    log("[*] 2. 授权前准备：提取 sso Cookie 并跨域注入...")
    sso_val = _try_document_cookie_sso()
    if not sso_val:
        log("[!] 未能在当前环境提取到 sso，请确保浏览器已登录！")
    else:
        log(f"[*] 成功提取 sso: {sso_val[:15]}...")

    try:
        run("open", "https://accounts.x.ai/account", "--window", "foreground", timeout=30)
        wait_doc_loaded(timeout=10)
        
        if sso_val:
            log("[*] 正在向 accounts.x.ai 注入跨域 Session Cookie...")
            inject_js = f"""(() => {{
                document.cookie = "sso={sso_val}; domain=.x.ai; path=/; secure; samesite=none";
                document.cookie = "sso-rw={sso_val}; domain=.x.ai; path=/; secure; samesite=none";
                document.cookie = "sso={sso_val}; domain=accounts.x.ai; path=/; secure; samesite=none";
                document.cookie = "sso-rw={sso_val}; domain=accounts.x.ai; path=/; secure; samesite=none";
                document.cookie = "sso={sso_val}; domain=auth.x.ai; path=/; secure; samesite=none";
                document.cookie = "sso-rw={sso_val}; domain=auth.x.ai; path=/; secure; samesite=none";
                return 'ok';
            }})()"""
            eval_js(inject_js)
            log("[*] Cookie 注入完成，刷新页面以同步会话状态...")
            time.sleep(1.0)
            run("open", "https://accounts.x.ai/account", "--window", "foreground", timeout=30)
            wait_doc_loaded(timeout=10)
            
        time.sleep(3.0)
    except Exception as e:
        log(f"[!] 导航至 accounts.x.ai 出现异常: {e}")

    log("[*] 3. 向 auth.x.ai 申请 Device Code...")
    dev_info = request_device_code(device_endpoint=device_ep)
    device_code = dev_info.get("device_code", "")
    user_code = dev_info.get("user_code", "")
    complete_url = dev_info.get("verification_uri_complete", "") or f"https://accounts.x.ai/oauth2/device?user_code={user_code}"
    interval = int(dev_info.get("interval", 5))

    if not device_code or not complete_url:
        raise Exception("获取 Device Code 失败")

    log(f"[*] 成功获取 User Code: {user_code}")
    log(f"[*] 导航访问授权页面: {complete_url}")

    run("open", complete_url, "--window", "foreground", timeout=30)
    wait_doc_loaded(timeout=10)

    deadline = time.time() + timeout
    device_authorized = False

    log("[*] 执行多阶段 JS 自动化进行设备确认与授权...")
    while time.time() < deadline:
        res = eval_js(f"""(() => {{
            const text = document.body ? document.body.innerText : '';
            const url = window.location.href;

            const ensureId = (el) => {{
                if (!el.id) el.id = '__grok_auth_btn_1785043974545';
                return el.id;
            }};

            // 阶段 3：已授权完成判定
            if (url.includes('/device/done') || text.includes('设备已授权') || text.includes('Device Authorized') || text.includes('成功连接')) {{
                return 'device_authorized';
            }}

            // 阶段 4：设备确认由于未登录跳到重新登录页面
            if (url.includes('/sign-in') || url.includes('login')) {{
                const buttons = Array.from(document.querySelectorAll('button'));
                const emailBtn = buttons.find(b => {{
                    const t = (b.innerText || '').trim();
                    return t === '使用邮箱登录' || t.includes('Continue with email') || t.includes('Sign in with email');
                }});
                if (emailBtn) {{
                    return 'found_signin_email_btn:' + ensureId(emailBtn);
                }}
                
                const emailInput = document.querySelector('input[type="email"], input[name="email"]');
                const passwordInput = document.querySelector('input[type="password"], input[name="password"]');
                
                if (emailInput && !passwordInput) {{
                    if (!emailInput.value) {{
                        emailInput.value = '{email}';
                        emailInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                    const nextBtn = buttons.find(b => {{
                        const t = (b.innerText || '').trim();
                        return t === '下一步' || t === 'Next' || t === 'Continue' || t === '继续';
                    }});
                    if (nextBtn) {{
                        return 'found_signin_next_btn:' + ensureId(nextBtn);
                    }}
                }}
                
                if (emailInput && passwordInput) {{
                    if (!emailInput.value) {{
                        emailInput.value = '{email}';
                        emailInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                    if (!passwordInput.value) {{
                        passwordInput.value = '{password}';
                        passwordInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                    const loginBtn = buttons.find(b => {{
                        const t = (b.innerText || '').trim();
                        return t === '登录' || t === 'Sign in' || t === 'Log in';
                    }});
                    if (loginBtn) {{
                        return 'found_signin_login_btn:' + ensureId(loginBtn);
                    }}
                }}
                return 'waiting_signin';
            }}

            // 阶段 1：设备确认页 (存在 input[name=user_code] 且非 consent 路由)
            const codeInput = document.querySelector('input[name=user_code]');
            if (codeInput && !url.includes('/consent')) {{
                if (!codeInput.value) {{
                    codeInput.value = '{user_code}';
                    codeInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
                const buttons = Array.from(document.querySelectorAll('button, input[type=submit]'));
                const nextBtn = buttons.find(b => {{
                    const t = (b.innerText || b.value || '').trim();
                    return t === '继续' || t === 'Continue' || t === 'Next' || t === '确认';
                }}) || document.querySelector('button[type=submit]');
                if (nextBtn) {{
                    return 'found_device_next:' + ensureId(nextBtn);
                }}
            }}

            // 阶段 2：授权 consent页 + Form Submit DOM 兜底
            if (url.includes('/consent') || text.includes('授权 Grok Build') || text.includes('Authorize Grok Build') || text.includes('Grok Build')) {{
                const buttons = Array.from(document.querySelectorAll('button, a, input[type=submit]'));
                const allowBtn = buttons.find(b => {{
                    const t = (b.innerText || b.value || '').trim();
                    return t === '允许' || t === 'Allow' || t === 'Authorize' || t === 'Approve';
                }});
                if (allowBtn) {{
                    if (!allowBtn.id) allowBtn.id = '__grok_auth_btn_1785043974545';
                    return 'found_allow:' + allowBtn.id;
                }}

                // DOM 提交兜底：找不到允许按钮或点击未触发跳转时，强制向 Form 插入 action=allow 并 submit
                const form = document.querySelector('form');
                if (form) {{
                    let actionInput = form.querySelector('input[name=action]');
                    if (!actionInput) {{
                        actionInput = document.createElement('input');
                        actionInput.type = 'hidden';
                        actionInput.name = 'action';
                        form.appendChild(actionInput);
                    }}
                    actionInput.value = 'allow';
                    form.submit();
                    return 'submitted_form_fallback';
                }}
            }}
            return 'waiting';
        }})()""")

        res_str = str(res)
        
        if res_str == "device_authorized":
            log("[*] 前端已确认【设备已授权】...")
            break
        elif res_str.startswith("found_signin_email_btn:"):
            btn_id = res_str.split(":", 1)[1]
            log(f"[*] 页面重定向至登录，找到【使用邮箱登录】按钮，执行原生点击...")
            try: run("click", f"#{btn_id}", timeout=5)
            except Exception as e: log(f"[!] 原生点击失败: {e}")
            time.sleep(2.0)
        elif res_str.startswith("found_signin_next_btn:"):
            btn_id = res_str.split(":", 1)[1]
            log(f"[*] 找到【下一步】按钮，执行原生点击...")
            try: run("click", f"#{btn_id}", timeout=5)
            except Exception as e: log(f"[!] 原生点击失败: {e}")
            time.sleep(2.0)
        elif res_str.startswith("found_signin_login_btn:"):
            btn_id = res_str.split(":", 1)[1]
            log(f"[*] 找到【登录】按钮，执行原生点击并加持 Cloudflare 人机验证...")
            try: run("click", f"#{btn_id}", timeout=5)
            except Exception as e: log(f"[!] 原生点击失败: {e}")
            time.sleep(1.5)
            log("[*] 主动触发 Turnstile (CDP 原生点击复选框)...")
            try: run("click", "#__cf_click_anchor", timeout=5)
            except Exception: pass
            time.sleep(4.0)
        elif res_str.startswith("found_allow:"):
            btn_id = res_str.split(":", 1)[1]
            log(f"[*] 找到授权允许按钮(id={btn_id})，执行原生 CDP 点击后，拉起chromium")
            try:
                run("click", f"#{btn_id}", timeout=10)
            except Exception as e:
                log(f"[!] 原生点击失败: {e}")
            time.sleep(2.0)
            
            log("[*] 正在拉起chromium访问 sign-in 并验证...")
            open_signin_and_login_with_email(email, password, log)
            break
        elif res_str == "submitted_form_fallback":
            log("[*] 触发 Form Submit DOM 兜底提交，等待页面刷新响应...")
            time.sleep(2.0)
            log("[*] 正在拉起chromium访问 sign-in 并验证...")
            open_signin_and_login_with_email(email, password, log)
            break
        elif res_str.startswith("found_device_next:"):
            btn_id = res_str.split(":", 1)[1]
            log(f"[*] 找到设备确认【继续】按钮(id={btn_id})，执行原生 CDP 点击...")
            try:
                run("click", f"#{btn_id}", timeout=10)
            except Exception as e:
                log(f"[!] 原生点击失败: {e}")
            time.sleep(2.5)
        else:
            time.sleep(1.5)

    log("[*] chromium登录完毕（或已跳过），开始获取 access token...")
    token_result = poll_device_token(device_code, interval=interval, timeout=60, token_endpoint=token_ep, log=log)
    
    if token_result and token_result.get("access_token"):
        # 自动写出标准 CPA xAI JSON 文件并落地
        save_cpa_json_credential(
            token_result,
            token_endpoint=token_ep,
            auth_dir="cpa_auths",
            log=log
        )
        return token_result.get("access_token")

    raise Exception("等待超时或授权失败：未能获取 Access Token")


# ─── NSFW 开启 (HTTP) ───────────────────────────────────────────────────────────


def _is_cloudflare_block(res):
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(res.headers).items()}
        text = str(res.text or "").lower()
        server = headers.get("server", "")
        return (
            res.status_code in (403, 429, 503)
            and ("cloudflare" in server or "cloudflare" in text or "__cf_chl" in text)
        )
    except Exception:
        return False


def _generate_birthdate():
    import datetime as dt
    today = dt.date.today()
    age = random.randint(20, 40)
    return f"{today.year - age}-{random.randint(1,12):02d}-{random.randint(1,28):02d}T16:00:00.000Z"


def enable_nsfw_for_token(token, log=print):
    from curl_cffi import requests as creq
    proxies = _get_proxies()
    ua = _get_user_agent()
    try:
        with creq.Session(impersonate="chrome120", proxies=proxies) as session:
            session.headers.update({
                "user-agent": ua,
                "cookie": f"sso={token}; sso-rw={token}",
            })

            url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
            payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
            data = b"\x00" + struct.pack(">I", len(payload)) + payload
            res = session.post(url, data=data, headers={
                "content-type": "application/grpc-web+proto",
                "x-grpc-web": "1",
                "x-user-agent": "connect-es/2.1.1",
                "origin": "https://accounts.x.ai",
                "referer": "https://accounts.x.ai/accept-tos",
            }, timeout=15)
            log(f"[Debug] set_tos status: {res.status_code}")
            if not (200 <= res.status_code < 300):
                return False, f"set_tos HTTP {res.status_code}"

            res = session.post(
                "https://grok.com/rest/auth/set-birth-date",
                json={"birthDate": _generate_birthdate()},
                headers={"content-type": "application/json", "origin": "https://grok.com", "referer": "https://grok.com/"},
                timeout=15,
            )
            log(f"[Debug] set_birth_date status: {res.status_code}")
            if not (200 <= res.status_code < 300):
                return False, f"set_birth_date HTTP {res.status_code}"

            field1_content = bytes([0x10, 0x01])
            field1 = bytes([0x0A, len(field1_content)]) + field1_content
            nsfw_string = b"always_show_nsfw_content"
            field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
            field2 = bytes([0x12, len(field2_inner)]) + field2_inner
            nsfw_payload = field1 + field2
            nsfw_data = b"\x00" + struct.pack(">I", len(nsfw_payload)) + nsfw_payload
            res = session.post(
                "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls",
                data=nsfw_data,
                headers={
                    "content-type": "application/grpc-web+proto",
                    "x-grpc-web": "1",
                    "origin": "https://grok.com",
                    "referer": "https://grok.com/",
                },
                timeout=15,
            )
            log(f"[Debug] update_nsfw status: {res.status_code}")
            if not (200 <= res.status_code < 300):
                return False, f"update_nsfw HTTP {res.status_code}"

            return True, "成功开启 NSFW"
    except Exception as e:
        return False, f"异常: {e}"


# ─── 账号输出 ───────────────────────────────────────────────────────────────────


def append_account_line(path, email, password, sso):
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{email}----{password}----{sso}\n")
        f.flush()
        os.fsync(f.fileno())


def save_mail_credential(base_dir, email, credential):
    path = os.path.join(base_dir, "mail_credentials.txt")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{email}\t{credential}\n")
        f.flush()
        os.fsync(f.fileno())


# ─── 批量编排 ───────────────────────────────────────────────────────────────────


def register_one_account(log=print, enable_nsfw=True, provider=None):
    log("[*] 1. 打开注册页")
    open_signup_page(log=log)

    log("[*] 2. 创建邮箱并提交")
    email, dev_token = get_email_and_token(provider=provider)
    log(f"[*] 邮箱: {email}")
    save_mail_credential(str(ROOT), email, dev_token)
    fill_email_and_submit(email, log=log)

    log("[*] 3. 拉取验证码")
    code = get_oai_code(dev_token, email, provider=provider, log=log)
    log(f"[*] 验证码: {code}")
    fill_code_and_submit(code, log=log)

    log("[*] 4. 填写资料")
    profile = fill_profile_and_submit(log=log)
    log(f"[*] 资料: {profile['given_name']} {profile['family_name']}")

    log("[*] 5. 授权 Grok Build")
    token = authorize_grok_build(email=email, password=profile["password"], log=log)
    log(f"[*] 授权成功, Access Token 长度: {len(token)}")

    if enable_nsfw:
        log("[*] 6. 开启 NSFW")
        try:
            ok, msg = enable_nsfw_for_token(token, log=log)
            log(f"[{'+' if ok else '!'}] NSFW: {msg}")
        except Exception as e:
            log(f"[!] 开启 NSFW 出现异常: {e}")

    return email, profile["password"], token


def run_batch(count, log=print, enable_nsfw=True, provider=None, output_path=None):
    if not output_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(ROOT / f"accounts_{ts}.txt")

    print(f"注册数量: {count}")
    print(f"输出文件: {output_path}")
    print(f"NSFW: {'开启' if enable_nsfw else '跳过'}")
    print(f"邮箱: {provider or cfg('email_provider', 'duckmail')}")
    print("=" * 50)

    ensure_browser_bridge()
    print("[bridge] 已连接")

    success = 0
    fail = 0
    for i in range(1, count + 1):
        print(f"\n{'#' * 50}")
        print(f"# [{i}/{count}]")
        print(f"{'#' * 50}")

        rc = 0
        for attempt in range(1, BRIDGE_RECOVER_RETRIES + 1):
            try:
                email, password, sso = register_one_account(
                    log=log, enable_nsfw=enable_nsfw, provider=provider
                )
                append_account_line(output_path, email, password, sso)
                log(f"[OK] 已保存: {email}")
                success += 1
                break
            except BridgeDisconnectedError as e:
                log(f"[bridge] 断线: {e}")
                if attempt >= BRIDGE_RECOVER_RETRIES:
                    log("[ABORT] Bridge 恢复失败")
                    fail += 1
                    rc = 1
                    break
                log(f"[bridge] 恢复中 ({attempt}/{BRIDGE_RECOVER_RETRIES})...")
                try:
                    ensure_browser_bridge(max_wait_sec=45)
                    log("[bridge] 恢复成功")
                except BridgeDisconnectedError:
                    fail += 1
                    rc = 1
                    break
            except Exception as e:
                log(f"[FAIL] {e}")
                fail += 1
                rc = 1
                break

        if rc == 0 and i < count:
            time.sleep(1)

    print("\n" + "=" * 50)
    print(f"汇总: 成功 {success} / 失败 {fail} / 共 {count}")
    print(f"输出: {output_path}")
    print("=" * 50)
    return 0 if fail == 0 else 1


# ─── CLI ────────────────────────────────────────────────────────────────────────


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Grok 注册机 (opencli browser bridge 版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python grok_register_opencli.py              # 使用 config.json\n"
            "  python grok_register_opencli.py -n 5         # 注册 5 个\n"
            "  python grok_register_opencli.py --no-nsfw    # 跳过 NSFW\n"
            "  python grok_register_opencli.py --provider cloudflare\n"
        ),
    )
    p.add_argument("-n", "--count", type=int, default=None,
                   help="注册数量 (默认读 config.json 的 register_count)")
    p.add_argument("--no-nsfw", action="store_true", help="跳过 NSFW")
    p.add_argument("--provider", choices=["duckmail", "yyds", "cloudflare", "cloudmail"],
                   default=None, help="覆盖邮箱服务商")
    p.add_argument("--proxy", default=None, help="覆盖 HTTP 代理")
    p.add_argument("--output", default=None, help="账号输出文件路径")
    p.add_argument("--debug", action="store_true", help="输出详细日志")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    load_config()

    if args.proxy:
        _config["proxy"] = args.proxy

    count = args.count or int(cfg("register_count", 1) or 1)
    enable_nsfw = not args.no_nsfw and bool(cfg("enable_nsfw", True))
    provider = args.provider

    log = print

    try:
        return run_batch(
            count,
            log=log,
            enable_nsfw=enable_nsfw,
            provider=provider,
            output_path=args.output,
        )
    except KeyboardInterrupt:
        print("\n[!] 用户中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
