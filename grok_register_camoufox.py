#!/usr/bin/env python3
"""
Grok 注册机 (Camoufox 指纹浏览器版)

同一代理半小时只用一次；代理池默认 127.0.0.1:1801-1850。
每次启动按 --count 注册 N 个账号后退出；冷却中的代理自动跳过。

用法:
  python grok_register_camoufox.py              # 默认注册 1 个
  python grok_register_camoufox.py -n 3         # 注册 3 个
  python grok_register_camoufox.py --dry-run    # 只看端口冷却状态
  python grok_register_camoufox.py --no-turnstile
  python grok_register_camoufox.py --headless
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import shutil
import socket
import string
import struct
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)
except Exception:
    pass

SIGNUP_URL = 'https://accounts.x.ai/sign-up?redirect=grok-com'
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / 'config.json'
COOLDOWN_PATH = ROOT / '.tmp' / 'proxy_cooldown.json'
DEFAULT_CAMOUFOX_PATH = (
    r'C:\Users\Administrator\AppData\Local\camoufox\camoufox\Cache'
    r'\browsers\official\152.0.4-beta.28-386fc2f4\camoufox.exe'
)
DUCKMAIL_API_BASE = 'https://api.duckmail.sbs'
YYDS_API_BASE = 'https://maliapi.215.im/v1'
CLIENT_ID = 'b1a00492-073a-47ea-816f-4c329264a828'

_config: dict = {}
_current_proxy: str = ''
_cf_domain_index = 0
_cloudmail_domain_index = 0
_driver = None  # type: Optional['CamoufoxDriver']


def load_config() -> dict:
    global _config
    if CONFIG_PATH.is_file():
        _config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    else:
        _config = {}
    return _config


def cfg(key: str, default=''):
    return _config.get(key, default)


def cfg_bool(key: str, default: bool = False) -> bool:
    v = _config.get(key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'yes', 'on')
    return default


def set_current_proxy(proxy_url: str) -> None:
    global _current_proxy
    _current_proxy = (proxy_url or '').strip()


def _get_proxies() -> dict:
    if not _current_proxy:
        return {}
    return {'http': _current_proxy, 'https': _current_proxy}


def _get_user_agent() -> str:
    return str(
        cfg(
            'user_agent',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
        )
    )


def _embed_js(js_template: str, **kwargs) -> str:
    result = js_template
    for k, v in kwargs.items():
        result = result.replace(f'%%{k}%%', json.dumps(v, ensure_ascii=False))
    return result


def extract_json(text: str):
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r'(\{.*\}|\[.*\])', s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None
    return None


def http_get(url, **kwargs):
    """返回 requests.Response（与 opencli / 邮箱 provider 一致）。

    优先走当前绑定代理；代理失败时回退直连（邮箱 API 等可直连）。
    """
    import requests

    kw = dict(kwargs)
    proxies = kw.pop("proxies", None)
    if proxies is None:
        proxies = _get_proxies() or None
    timeout = kw.pop("timeout", 20)
    s = requests.Session()
    s.trust_env = False
    if proxies:
        try:
            return s.get(url, proxies=proxies, timeout=timeout, **kw)
        except Exception:
            pass
    return s.get(url, timeout=timeout, **kw)


def http_post(url, **kwargs):
    """返回 requests.Response（与 opencli / 邮箱 provider 一致）。"""
    import requests

    kw = dict(kwargs)
    # 兼容旧调用 json_body=
    if "json_body" in kw and "json" not in kw:
        kw["json"] = kw.pop("json_body")
    else:
        kw.pop("json_body", None)
    proxies = kw.pop("proxies", None)
    if proxies is None:
        proxies = _get_proxies() or None
    timeout = kw.pop("timeout", 20)
    s = requests.Session()
    s.trust_env = False
    if proxies:
        try:
            return s.post(url, proxies=proxies, timeout=timeout, **kw)
        except Exception:
            pass
    return s.post(url, timeout=timeout, **kw)


def parse_ports_spec(spec: str) -> list[int]:
    spec = (spec or '').strip()
    if not spec:
        start = int(cfg('proxy_port_start', 1801) or 1801)
        end = int(cfg('proxy_port_end', 1850) or 1850)
        return list(range(start, end + 1))
    ports: list[int] = []
    for part in spec.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            ports.extend(range(int(a), int(b) + 1))
        else:
            ports.append(int(part))
    return sorted(set(ports))


def proxy_url_for_port(port: int) -> str:
    return f'http://127.0.0.1:{port}'


def load_cooldown() -> dict[str, float]:
    if not COOLDOWN_PATH.is_file():
        return {}
    try:
        data = json.loads(COOLDOWN_PATH.read_text(encoding='utf-8'))
        out: dict[str, float] = {}
        for k, v in (data or {}).items():
            try:
                out[str(k)] = float(v)
            except Exception:
                continue
        return out
    except Exception:
        return {}


def save_cooldown(data: dict[str, float]) -> None:
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = COOLDOWN_PATH.with_suffix('.tmp')
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp_path, COOLDOWN_PATH)


def cooldown_sec() -> int:
    return int(cfg('ip_cooldown_sec', 1800) or 1800)


def remaining_cooldown(port: int, data: dict[str, float] | None = None) -> float:
    data = data if data is not None else load_cooldown()
    last = data.get(str(port))
    if last is None:
        return 0.0
    left = cooldown_sec() - (time.time() - float(last))
    return max(0.0, left)


def mark_port_used(port: int) -> None:
    data = load_cooldown()
    data[str(port)] = time.time()
    save_cooldown(data)


def probe_proxy_port(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            return True
    except OSError:
        return False


def list_candidate_ports(ports: list[int], log=print) -> list[int]:
    data = load_cooldown()
    available = []
    cooling = []
    dead = []
    for p in ports:
        left = remaining_cooldown(p, data)
        if left > 0:
            cooling.append((p, left))
            continue
        if not probe_proxy_port(p):
            dead.append(p)
            continue
        available.append(p)
    if cooling:
        preview = ', '.join(f'{p}({int(left)}s)' for p, left in cooling[:8])
        more = '' if len(cooling) <= 8 else f' ...共{len(cooling)}个'
        log(f'[*] 冷却中跳过: {preview}{more}')
    if dead:
        log(f'[*] 探活失败跳过(不记冷却): {dead[:12]}' + ('...' if len(dead) > 12 else ''))
    log(f'[*] 可用代理端口: {len(available)}/{len(ports)} -> {available[:20]}' + ('...' if len(available) > 20 else ''))
    return available


def resolve_camoufox_window() -> tuple[int, int]:
    """有头模式窗口尺寸。Camoufox 默认按指纹随机，可能超出显示器。

    config:
      camoufox_window: "1024x680" | "auto" | ""
    auto/空: 取主屏约 55%，夹在 960x600 ~ 1152x720。
    """
    raw = str(cfg("camoufox_window", "1024x680") or "1024x680").strip().lower()
    if raw and raw not in ("auto", "default"):
        m = re.match(r"^(\d+)\s*[xX,]\s*(\d+)$", raw)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w >= 800 and h >= 560:
                return w, h
    # auto：按主显示器收缩，避免超屏
    try:
        from screeninfo import get_monitors

        mon = get_monitors()[0]
        w = max(960, min(1152, int(mon.width * 0.55)))
        h = max(600, min(720, int(mon.height * 0.58)))
        # 偶数更稳
        return w - (w % 2), h - (h % 2)
    except Exception:
        return 1024, 680


class CamoufoxDriver:
    def __init__(self, proxy_url: str, headless: bool = False, executable_path: str = ''):
        self.proxy_url = proxy_url
        self.headless = headless
        self.executable_path = executable_path or str(cfg('camoufox_path', '') or '')
        self.window_size: tuple[int, int] = (1024, 680)
        self._cm = None
        self.browser = None
        self.context = None
        self.page = None


    def start(self):
        """启动 Camoufox。

        注意：不要用 camoufox.NewContext() —— 它会把 Playwright viewport 设成
        指纹 screen 全分辨率（常见 1920x1080），Juggler 随后把窗口从启动时的
        小尺寸撑到几乎全屏。启动指纹 + browser.new_page(no_viewport) 即可。
        """
        from camoufox.sync_api import Camoufox

        win_w, win_h = resolve_camoufox_window()
        self.window_size = (win_w, win_h)
        # outer 窗口；viewport 略小于 outer（扣掉标题栏/边框）
        vp_w = max(800, win_w - 16)
        vp_h = max(560, win_h - 88)
        opts: dict[str, Any] = {
            "headless": self.headless,
            "humanize": True,
            "proxy": {"server": self.proxy_url},
            # 固定窗口，避免指纹随机出超大 outer 尺寸撑爆屏幕
            "window": (win_w, win_h),
            # 显式写入 config，防止后续被其它默认值覆盖
            "config": {
                "window.outerWidth": win_w,
                "window.outerHeight": win_h,
                "window.innerWidth": vp_w,
                "window.innerHeight": vp_h,
                "screen.width": max(win_w + 40, 1100),
                "screen.height": max(win_h + 60, 720),
                "screen.availWidth": max(win_w + 40, 1100),
                "screen.availHeight": max(win_h + 20, 680),
            },
            # 我们故意钉死窗口尺寸
            "i_know_what_im_doing": True,
        }
        if not self.headless:
            print(f"[*] Camoufox 窗口: {win_w}x{win_h} (viewport {vp_w}x{vp_h})")
        if self.proxy_url:
            opts["geoip"] = True
        # 约束 BrowserForge 生成范围，与 window 一致
        try:
            from browserforge.fingerprints import Screen

            opts["screen"] = Screen(
                max_width=max(win_w + 40, 1100),
                max_height=max(win_h + 60, 720),
            )
        except Exception:
            pass

        exe = self.executable_path.strip()
        if exe and Path(exe).is_file():
            opts["executable_path"] = exe

        def _launch(launch_opts: dict[str, Any]):
            cm = Camoufox(**launch_opts)
            launched = cm.__enter__()
            return cm, launched

        try:
            self._cm, launched = _launch(opts)
        except Exception as e:
            if self.proxy_url and opts.get("geoip"):
                print(f"[!] geoip 启动失败，降级无 geoip 重试: {e}")
                opts.pop("geoip", None)
                self._cm, launched = _launch(opts)
            else:
                raise

        # Camoufox() 默认返回 Browser；指纹已在 launch 注入
        if hasattr(launched, "new_page") and not hasattr(launched, "add_cookies"):
            self.browser = launched
            self.context = None
            self.page = None
            # 禁止 NewContext：其 fingerprint viewport=screen 会把窗口撑大
            # browser.new_page 已被 attach_no_viewport_default 包过
            try:
                self.page = launched.new_page()
                self.context = self.page.context
            except Exception as e:
                print(f"[!] browser.new_page 失败，尝试 new_context(no_viewport): {e}")
                self.context = launched.new_context(no_viewport=True)
                self.page = self.context.new_page()
        else:
            # 已是 BrowserContext（persistent）
            self.browser = None
            self.context = launched
            pages = list(getattr(launched, "pages", []) or [])
            self.page = pages[0] if pages else launched.new_page()

        self.page.set_default_timeout(45000)
        self.page.set_default_navigation_timeout(60000)
        # 仅启动时强制一次 viewport；后续 goto 只 resizeTo，避免打断请求
        self.pin_window_size(force_viewport=True)
        return self

    def pin_window_size(self, force_viewport: bool = False) -> None:
        """把窗口钉回配置尺寸。

        注意：不要在表单提交 loading 中频繁 set_viewport_size，
        否则可能打断 XHR / 导致按钮一直转圈。
        """
        if self.page is None or self.headless:
            return
        win_w, win_h = self.window_size
        vp_w = max(800, win_w - 16)
        vp_h = max(560, win_h - 88)
        if force_viewport:
            try:
                self.page.set_viewport_size({"width": vp_w, "height": vp_h})
            except Exception:
                pass
        try:
            self.page.evaluate(
                """([w, h]) => {
try { if (Math.abs((window.outerWidth||0)-w)>8 || Math.abs((window.outerHeight||0)-h)>8) window.resizeTo(w, h); } catch (e) {}
return [window.outerWidth, window.outerHeight];
}""",
                [win_w, win_h],
            )
        except Exception:
            pass


    def close(self):
        try:
            if self.context and self.browser is not None:
                try:
                    self.context.close()
                except Exception:
                    pass
        finally:
            if self._cm is not None:
                try:
                    self._cm.__exit__(None, None, None)
                except Exception:
                    pass
            self._cm = None
            self.browser = None
            self.context = None
            self.page = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()

    def goto(self, url: str, wait_until: str = 'domcontentloaded', timeout: int = 60000):
        assert self.page is not None
        self.page.goto(url, wait_until=wait_until, timeout=timeout)
        # 导航后有时会被站点/viewport 再次拉大，钉回
        self.pin_window_size()


    def eval_js(self, js: str, timeout: int = 15000):
        assert self.page is not None
        script = (js or "").strip()
        if not script:
            return None
        # Playwright evaluate: 直接执行表达式/IIFE；不要二次包装，避免 (()=>...)() 被拆坏
        try:
            self.page.set_default_timeout(timeout)
        except Exception:
            pass
        return self.page.evaluate(script)

    def fill(self, selector: str, value: str, timeout: int = 15000) -> bool:
        """Playwright 原生填充，适配受控组件。"""
        assert self.page is not None
        loc = self.page.locator(selector).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            loc.click(timeout=timeout)
            loc.fill(value, timeout=timeout)
            return True
        except Exception:
            return False

    def type_text(self, selector: str, value: str, timeout: int = 15000) -> bool:
        assert self.page is not None
        loc = self.page.locator(selector).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            loc.click(timeout=min(5000, timeout))
            loc.fill("")
            loc.type(value, delay=30, timeout=timeout)
            return True
        except Exception:
            return False

    def click_text(self, texts: list[str], timeout: int = 5000) -> str:
        """按可见文案点击按钮/链接，成功返回匹配文案。"""
        assert self.page is not None
        for t in texts:
            # exact / contains
            for role in ("button", "link"):
                try:
                    loc = self.page.get_by_role(role, name=re.compile(re.escape(t), re.I))
                    if loc.count() > 0:
                        loc.first.click(timeout=timeout)
                        return t
                except Exception:
                    pass
            try:
                loc = self.page.get_by_text(t, exact=False)
                if loc.count() > 0:
                    loc.first.click(timeout=timeout)
                    return t
            except Exception:
                pass
        return ""

    def dump_debug(self, tag: str = "debug") -> str:
        """失败时落盘截图+HTML，返回摘要。"""
        assert self.page is not None
        out_dir = ROOT / ".tmp" / "debug"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = out_dir / f"{tag}_{ts}"
        summary = f"url={self.url()}"
        try:
            self.page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            summary += f" shot={base.with_suffix('.png').name}"
        except Exception as e:
            summary += f" shot_err={e}"
        try:
            html = self.page.content()
            base.with_suffix(".html").write_text(html, encoding="utf-8")
            summary += f" html={base.with_suffix('.html').name}"
        except Exception as e:
            summary += f" html_err={e}"
        try:
            info = self.page.evaluate(
                """() => ({
                  title: document.title,
                  inputs: Array.from(document.querySelectorAll('input,textarea')).slice(0,30).map(el => ({
                    tag: el.tagName, type: el.type||'', name: el.name||'', id: el.id||'',
                    testid: el.getAttribute('data-testid')||'', placeholder: el.placeholder||'',
                    aria: el.getAttribute('aria-label')||'', visible: !!(el.offsetWidth||el.offsetHeight)
                  })),
                  buttons: Array.from(document.querySelectorAll('button,a,[role=button]')).slice(0,30).map(el => ({
                    text: (el.innerText||el.textContent||'').trim().slice(0,80),
                    visible: !!(el.offsetWidth||el.offsetHeight)
                  }))
                })"""
            )
            base.with_suffix(".json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            summary += f" meta={base.with_suffix('.json').name}"
        except Exception as e:
            summary += f" meta_err={e}"
        return summary
    def click(self, selector: str, timeout: int = 10000):
        assert self.page is not None
        self.page.click(selector, timeout=timeout)

    def url(self) -> str:
        assert self.page is not None
        return self.page.url or ''

    def wait_load(self, timeout: float = 20.0):
        assert self.page is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = self.page.evaluate('document.readyState')
                if state in ('interactive', 'complete'):
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def cookies(self, url: str = 'https://grok.com') -> list[dict]:
        assert self.context is not None
        try:
            return self.context.cookies([url])
        except Exception:
            return self.context.cookies()


def get_driver() -> CamoufoxDriver:
    if _driver is None:
        raise RuntimeError('浏览器驱动未启动')
    return _driver


def eval_js(js: str, timeout: int = 15000):
    return get_driver().eval_js(js, timeout=timeout)


def get_current_url() -> str:
    try:
        return get_driver().url()
    except Exception:
        return ''


def wait_doc_loaded(timeout: float = 20.0):
    return get_driver().wait_load(timeout=timeout)

# ─── 邮箱 Provider（移植自 grok_register_opencli）────────────────────────────

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


def _normalize_otp_code(raw) -> str | None:
    """统一成可填入的码：优先保留 XXX-XXX，否则 6 位连写也可。"""
    if raw is None:
        return None
    s = str(raw).strip().upper().replace(" ", "")
    if not s:
        return None
    m = re.fullmatch(r"([A-Z0-9]{3})-([A-Z0-9]{3})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.fullmatch(r"([A-Z0-9]{6})", s)
    if m:
        # xAI 页面多半接受无连字符；展示仍插回连字符
        return f"{s[:3]}-{s[3:]}"
    m = re.fullmatch(r"(\d{4,8})", s)
    if m:
        return s
    return None


def extract_verification_code(text, subject=""):
    """从主题/正文/字段提取 xAI 验证码。兼容 SpaceXAI confirmation code: XXX-XXX。"""
    blobs = [str(subject or ""), str(text or "")]
    joined = "\n".join(blobs)

    # 1) 主题常见：SpaceXAI confirmation code: 2A7-RCY  /  2A7-RCY xAI
    for blob in blobs:
        if not blob:
            continue
        for pat in (
            r"confirmation\s+code[:\s]+([A-Z0-9]{3}-[A-Z0-9]{3})",
            r"confirmation\s+code[:\s]+([A-Z0-9]{6})",
            r"(?:security|verification|one[-\s]?time)\s+code[:\s]+([A-Z0-9]{3}-[A-Z0-9]{3})",
            r"(?:security|verification|one[-\s]?time)\s+code[:\s]+([A-Z0-9]{6})",
            r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+(?:xAI|SpaceXAI)",
            r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b",
        ):
            m = re.search(pat, blob, re.IGNORECASE | re.MULTILINE)
            if m:
                code = _normalize_otp_code(m.group(1))
                if code:
                    return code

    # 2) 正文 hyphen 码
    m = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", joined, re.IGNORECASE)
    if m:
        return _normalize_otp_code(m.group(1))

    # 3) 纯数字备用
    for pattern in (
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ):
        m = re.search(pattern, joined, re.IGNORECASE)
        if m:
            return _normalize_otp_code(m.group(1))
    return None


def code_from_mail_object(msg: dict | None) -> str | None:
    """列表/详情对象直接抽码（YYDS 常带 verificationCode 字段）。"""
    if not isinstance(msg, dict):
        return None
    for key in (
        "verificationCode",
        "verification_code",
        "code",
        "otp",
        "securityCode",
        "security_code",
    ):
        code = _normalize_otp_code(msg.get(key))
        if code:
            return code
    # 嵌套 data
    data = msg.get("data")
    if isinstance(data, dict):
        nested = code_from_mail_object(data)
        if nested:
            return nested
    subject = str(msg.get("subject") or "")
    body = normalize_mail_body(msg)
    return extract_verification_code(body, subject)


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
    addr_l = (address or "").lower()
    last_status = 0.0
    while time.time() < deadline:
        try:
            messages = yyds_get_messages(address, token=token)
        except Exception as exc:
            log(f"[Debug] YYDS 拉取邮件列表失败: {exc}")
            time.sleep(poll_interval)
            continue
        now = time.time()
        if now - last_status >= 15:
            log(f"[*] YYDS 轮询中... 已收到 {len(messages or [])} 封, 剩余 {int(deadline - now)}s")
            last_status = now
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id") or msg.get("msgid") or ""
            # to 过滤放宽：无 to 字段也接受；有则需命中
            to_list = msg.get("to") or []
            if to_list:
                to_addrs = []
                for t in to_list:
                    if isinstance(t, dict):
                        to_addrs.append(str(t.get("address") or "").lower())
                    else:
                        to_addrs.append(str(t).lower())
                if addr_l and addr_l not in to_addrs and not any(addr_l in x for x in to_addrs):
                    continue

            # 列表项常已带 verificationCode / subject 含码 —— 优先直接用，不必等详情
            code = code_from_mail_object(msg)
            if code:
                log(f"[*] YYDS 验证码(列表): {code} subject={msg.get('subject', '')}")
                return code

            if not msg_id:
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
            subject = ""
            if isinstance(detail, dict):
                subject = str(detail.get("subject") or msg.get("subject") or "")
                code = code_from_mail_object(detail) or extract_verification_code(
                    normalize_mail_body(detail), subject
                )
            else:
                code = None
            log(f"[Debug] YYDS 收到邮件: {subject}")
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


# ─── 资料生成 ────────────────────────────────────────────────────────────────

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


# ─── 浏览器步骤 ────────────────────────────────────────────────────────────────



def clear_browser_session(log=print):
    """只清 cookie，不再多站点乱跳（避免 CF/横幅/会话打乱）。"""
    d = get_driver()
    log("[*] 清理浏览器 cookie...")
    try:
        ctx = d.context or (d.page.context if d.page else None)
        if ctx:
            ctx.clear_cookies()
    except Exception as e:
        log(f"[!] clear_cookies: {e}")
    try:
        if d.page:
            d.page.evaluate(
                """() => {
try { localStorage.clear(); } catch (e) {}
try { sessionStorage.clear(); } catch (e) {}
return true;
}"""
            )
    except Exception:
        pass


def _dismiss_cookie_banner(log=print):
    d = get_driver()
    texts = (
        "接受所有 Cookie",
        "Accept All Cookies",
        "Accept all cookies",
        "Accept all",
        "全部允许",
        "Allow all",
        "Terima Semua Kuki",
        "Benarkan Semua",
        "Tolak Semua",
        "Accept All",
    )
    for text in texts:
        try:
            loc = d.page.get_by_role("button", name=re.compile(re.escape(text), re.I))
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=2000)
                log(f"[*] 已关闭 Cookie 横幅: {text}")
                time.sleep(0.5)
                return True
        except Exception:
            pass
        try:
            loc = d.page.get_by_text(text, exact=False)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=2000)
                log(f"[*] 已关闭 Cookie 横幅: {text}")
                time.sleep(0.5)
                return True
        except Exception:
            pass
    return False


def wait_code_input(timeout=30, log=print) -> bool:
    """提交邮箱后等待验证码输入框（name=code / OTP）。"""
    d = get_driver()
    selectors = [
        'input[name="code"]',
        'input[data-input-otp="true"]',
        'input[autocomplete="one-time-code"]',
        'input[data-testid="code"]',
        'input[inputmode="numeric"]',
    ]
    deadline = time.time() + timeout
    while time.time() < deadline:
        _dismiss_cookie_banner(log=log)
        for sel in selectors:
            try:
                loc = d.page.locator(sel)
                if loc.count() < 1:
                    continue
                item = loc.first
                item.wait_for(state="visible", timeout=800)
                if item.is_visible():
                    return True
            except Exception:
                continue
        # 多框 OTP
        try:
            boxes = d.page.locator('input[maxlength="1"]')
            if boxes.count() >= 4 and boxes.first.is_visible():
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def page_error_hints(log=print) -> list[str]:
    try:
        d = get_driver()
        hints = d.page.evaluate(
            """() => Array.from(document.querySelectorAll('[role="alert"], [data-testid*="error" i], .error, p, span, div'))
              .map(e => (e.innerText||'').trim())
              .filter(t => t && t.length < 200 && /error|invalid|fail|block|unable|无法|错误|无效|已存在|exist|limit|not allowed|denied|try again/i.test(t))
              .slice(0, 8)"""
        )
        return list(hints or [])
    except Exception:
        return []


def is_email_submit_loading() -> bool:
    """注册按钮是否处于 loading（转圈/disabled/空文本+svg）。"""
    try:
        d = get_driver()
        return bool(
            d.page.evaluate(
                """() => {
const buttons = Array.from(document.querySelectorAll('button'));
// 主提交区：含注册/Sign up，或正在转圈的大按钮
for (const b of buttons) {
  if (!b.offsetWidth || !b.offsetHeight) continue;
  const t = (b.innerText || b.textContent || '').replace(/\\s+/g, '').toLowerCase();
  const isSignup = t === '注册' || t === 'signup' || t.includes('signup') || t === 'continue' || t === '继续';
  const hasSvg = !!b.querySelector('svg');
  const busy = b.disabled || b.getAttribute('aria-busy') === 'true' || b.getAttribute('data-loading') === 'true';
  const emptySpin = hasSvg && t.length === 0 && b.offsetWidth > 80;
  if ((isSignup || emptySpin) && (busy || emptySpin)) return true;
}
// 仍在邮箱页且有 progressbar
if (document.querySelector('[role="progressbar"]')) return true;
return false;
}"""
            )
        )
    except Exception:
        return False


def click_email_submit_once(log=print) -> str:
    """只点一次注册/提交，返回点击到的文案或空串。"""
    d = get_driver()
    for text in ("注册", "Sign up", "继续", "Continue", "下一步", "Next", "Create account"):
        try:
            loc = d.page.get_by_role("button", name=re.compile("^" + re.escape(text) + "$", re.I))
            if loc.count() == 0:
                loc = d.page.get_by_role("button", name=re.compile(re.escape(text), re.I))
            if loc.count() < 1:
                continue
            btn = loc.first
            if not btn.is_visible():
                continue
            try:
                if btn.is_disabled():
                    continue
            except Exception:
                pass
            # 人类点击更稳（Camoufox humanize）
            try:
                btn.click(timeout=5000, no_wait_after=True)
            except TypeError:
                btn.click(timeout=5000)
            log(f"[*] 已提交邮箱: 点击「{text}」")
            return text
        except Exception:
            continue
    try:
        d.page.locator('form button[type="submit"], button[type="submit"]').first.click(
            timeout=3000, no_wait_after=True
        )
        log("[*] 已提交邮箱: submit 按钮")
        return "submit"
    except TypeError:
        try:
            d.page.locator('form button[type="submit"], button[type="submit"]').first.click(timeout=3000)
            log("[*] 已提交邮箱: submit 按钮")
            return "submit"
        except Exception:
            pass
    except Exception:
        pass
    try:
        d.page.keyboard.press("Enter")
        log("[*] 已提交邮箱: Enter")
        return "enter"
    except Exception:
        return ""


def open_signup_page(log=print):
    d = get_driver()
    clear_browser_session(log=log)
    log("[*] 打开注册页...")
    last_err = None
    for attempt in range(1, 4):
        try:
            d.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            d.wait_load(15)
        except Exception as e:
            last_err = e
            log(f"[!] goto signup 失败({attempt}): {e}")
            time.sleep(1)
            continue
        curr = d.url()
        log(f"[*] 当前URL: {curr}")
        if "accounts.x.ai" in (curr or "").lower():
            break
        log(f"[*] 仍未到达 accounts.x.ai (第{attempt}次)")
    else:
        dbg = ""
        try:
            dbg = d.dump_debug("signup_nav_fail")
        except Exception:
            pass
        raise Exception(f"无法到达 accounts.x.ai 注册页，当前 URL: {d.url()} last={last_err} {dbg}")

    click_email_signup_button(log=log)
    # 点完后必须等到邮箱框
    if not wait_email_input(timeout=25, log=log):
        dbg = ""
        try:
            dbg = d.dump_debug("no_email_input_after_click")
        except Exception:
            pass
        raise Exception(f"点击使用邮箱注册后未出现邮箱输入框 url={d.url()} {dbg}")


def wait_email_input(timeout=25, log=print):
    d = get_driver()
    selectors = [
        'input[data-testid="email"]',
        'input[name="email"]',
        'input[type="email"]',
        'input[autocomplete="email"]',
    ]
    deadline = time.time() + timeout
    while time.time() < deadline:
        _dismiss_cookie_banner(log=log)
        for sel in selectors:
            try:
                loc = d.page.locator(sel)
                if loc.count() < 1:
                    continue
                item = loc.first
                item.wait_for(state="visible", timeout=800)
                if item.is_visible():
                    return True
            except Exception:
                continue
        time.sleep(0.4)
    return False


def click_email_signup_button(timeout=25, log=print):
    d = get_driver()
    deadline = time.time() + timeout
    # 已在邮箱页
    if wait_email_input(timeout=2, log=log):
        log("[*] 已处于邮箱填写页面")
        return True

    texts = [
        "使用邮箱注册",
        "使用电子邮件注册",
        "Sign up with email",
        "Continue with email",
        "Sign up with Email",
    ]
    while time.time() < deadline:
        curr = get_current_url()
        if "accounts.x.ai" not in (curr or "").lower():
            time.sleep(0.8)
            continue

        if wait_email_input(timeout=1.5, log=log):
            log("[*] 已处于邮箱填写页面")
            return True

        _dismiss_cookie_banner(log=log)

        # 精确文案：button/link
        for text in texts:
            try:
                loc = d.page.get_by_role("button", name=re.compile("^" + re.escape(text) + "$", re.I))
                if loc.count() == 0:
                    loc = d.page.get_by_role("button", name=re.compile(re.escape(text), re.I))
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=5000)
                    log(f"[*] 已点击「使用邮箱注册」: {text}")
                    time.sleep(1.5)
                    if wait_email_input(timeout=8, log=log):
                        return True
            except Exception as e:
                log(f"[Debug] role click {text}: {e}")

        # get_by_text 精确优先
        for text in texts:
            try:
                loc = d.page.get_by_text(text, exact=True)
                if loc.count() == 0:
                    loc = d.page.get_by_text(text, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=5000)
                    log(f"[*] 已点击「使用邮箱注册」(text): {text}")
                    time.sleep(1.5)
                    if wait_email_input(timeout=8, log=log):
                        return True
            except Exception as e:
                log(f"[Debug] text click {text}: {e}")

        # JS 兜底
        try:
            clicked = eval_js(
                r"""(() => {
function isVisible(node) {
  if (!node) return false;
  const s = getComputedStyle(node);
  if (s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
  const r = node.getBoundingClientRect();
  return r.width>0 && r.height>0;
}
const want = ['使用邮箱注册','使用电子邮件注册','signup with email','continue with email'];
const nodes = Array.from(document.querySelectorAll('button,a,[role="button"]'));
for (const n of nodes) {
  if (!isVisible(n) || n.disabled) continue;
  const t = (n.innerText||n.textContent||'').replace(/\s+/g,'').toLowerCase();
  if (want.some(w => t.includes(w.replace(/\s+/g,'').toLowerCase()))) {
    n.click();
    return (n.innerText||n.textContent||'').trim() || 'clicked';
  }
}
return '';
})()"""
            )
            if clicked:
                log(f"[*] 已点击「使用邮箱注册」(JS): {clicked}")
                time.sleep(1.5)
                if wait_email_input(timeout=8, log=log):
                    return True
        except Exception as e:
            log(f"[Debug] click_email JS: {e}")
        time.sleep(0.8)

    dbg = ""
    try:
        dbg = d.dump_debug("no_email_signup_btn")
    except Exception:
        pass
    raise Exception(f"未找到「使用邮箱注册」按钮 {dbg}")


def _react_fill_email(email: str) -> dict:
    """用原生 value setter + InputEvent 写入，兼容 React 受控组件。"""
    d = get_driver()
    return d.page.evaluate(
        """(email) => {
const sels = [
  'input[data-testid="email"]','input[name="email"]',
  'input[type="email"]','input[autocomplete="email"]'
];
let input = null;
for (const s of sels) {
  const el = document.querySelector(s);
  if (el && el.offsetWidth && !el.disabled) { input = el; break; }
}
if (!input) return {ok:false, reason:'no-input'};
input.focus();
input.click();
const proto = input instanceof HTMLTextAreaElement
  ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (setter) setter.call(input, email); else input.value = email;
input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, data:email, inputType:'insertText'}));
input.dispatchEvent(new InputEvent('input', {bubbles:true, data:email, inputType:'insertText'}));
input.dispatchEvent(new Event('change', {bubbles:true}));
input.blur();
const valid = (input.type || '').toLowerCase() !== 'email' || input.checkValidity();
return {
  ok: (input.value || '').trim() === email && valid,
  value: input.value || '',
  valid
};
}""",
        email,
    )


def _wait_turnstile_quiet(timeout: float = 8.0, log=print) -> None:
    """提交前稍等隐形 Turnstile / challenge 脚本就绪。"""
    d = get_driver()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            st = d.page.evaluate(
                """() => {
const tok = document.querySelector('input[name="cf-turnstile-response"]');
const v = tok ? String(tok.value || '') : '';
const hasTs = Array.from(document.querySelectorAll('iframe')).some(f => /turnstile|challenges\\.cloudflare/i.test(f.src||''));
const ready = !!(window.turnstile);
return {tokenLen: v.length, hasTs, ready};
}"""
            )
            if isinstance(st, dict) and (st.get("tokenLen", 0) > 10 or (st.get("ready") and time.time() + 3 > deadline)):
                if st.get("tokenLen", 0) > 10:
                    log(f"[*] Turnstile token 就绪 len={st.get('tokenLen')}")
                    return
        except Exception:
            pass
        time.sleep(0.4)
    # 不等到也继续，很多情况下是隐式通过


def fill_email_and_submit(email, timeout=60, log=print):
    """填邮箱并点注册；loading 中绝不重点；失败整页重试一次。"""
    d = get_driver()
    email = str(email).strip()
    if not email:
        raise Exception("邮箱为空")

    last_err = ""
    for round_i in range(1, 3):
        if round_i > 1:
            log(f"[*] 邮箱提交第 {round_i} 次尝试（整页重开）...")
            try:
                open_signup_page(log=log)
            except Exception as e:
                last_err = str(e)

        if not wait_email_input(timeout=15, log=log):
            log("[*] 邮箱框未就绪，重新打开注册流...")
            open_signup_page(log=log)
            if not wait_email_input(timeout=15, log=log):
                continue

        _dismiss_cookie_banner(log=log)
        time.sleep(0.3)

        # 1) React 兼容写入
        filled_ok = False
        for attempt in range(1, 4):
            try:
                raw = _react_fill_email(email)
                if isinstance(raw, dict) and raw.get("ok"):
                    filled_ok = True
                    log(f"[*] 邮箱已填入(React): {email}")
                    break
                # Playwright 兜底
                loc = d.page.locator('input[data-testid="email"], input[type="email"]').first
                loc.click(timeout=4000)
                loc.fill("")
                loc.press_sequentially(email, delay=25)
                got = (loc.input_value(timeout=2000) or "").strip()
                if got == email:
                    filled_ok = True
                    log(f"[*] 邮箱慢速填入: {email}")
                    break
                last_err = f"fill={raw} got={got!r}"
            except Exception as e:
                last_err = str(e)
                log(f"[Debug] fill attempt {attempt}: {e}")
            time.sleep(0.4)

        if not filled_ok:
            if round_i < 2:
                continue
            dbg = ""
            try:
                dbg = d.dump_debug("fill_email_value_fail")
            except Exception as e:
                dbg = f"dump_err={e}"
            raise Exception(f"邮箱未能写入输入框 email={email} {last_err} {dbg}")

        # 2) 等按钮可点 + 给 CF 一点时间
        time.sleep(0.8)
        _wait_turnstile_quiet(timeout=6.0, log=log)

        if wait_code_input(timeout=1.0, log=log):
            log("[*] 已处于验证码页")
            return True

        # 3) 点一次，并尽量等到 CreateEmailValidationCode
        api_status = None
        clicked = ""
        try:
            with d.page.expect_response(
                lambda r: ("CreateEmailValidationCode" in (r.url or ""))
                or ("EmailValidation" in (r.url or "")),
                timeout=35000,
            ) as ri:
                clicked = click_email_submit_once(log=log)
                if not clicked:
                    # JS 点
                    clicked = (
                        d.page.evaluate(
                            """() => {
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const btn = buttons.find(b => {
  if (!b.offsetWidth || b.disabled) return false;
  const t = (b.innerText||b.textContent||'').replace(/\\s+/g,'').toLowerCase();
  return t==='注册'||t==='signup'||t.includes('signup')||t==='continue'||t==='继续';
});
if (!btn) return '';
btn.click();
return (btn.innerText||'js').trim()||'js';
}"""
                        )
                        or ""
                    )
                    if clicked:
                        log(f"[*] 已提交邮箱(JS): {clicked}")
            try:
                resp = ri.value
                api_status = getattr(resp, "status", None)
                log(f"[*] 注册 API 响应: status={api_status}")
            except Exception:
                pass
        except Exception as e:
            # 超时也可能已跳页
            last_err = str(e)
            log(f"[Debug] 等待注册 API: {e}")
            if not clicked:
                clicked = click_email_submit_once(log=log)

        if not clicked and not wait_code_input(timeout=2, log=log):
            last_err = "no-click"
            continue

        # 4) loading 中只等，绝不重点
        deadline = time.time() + 45
        last_log = 0.0
        while time.time() < deadline:
            if wait_code_input(timeout=1.2, log=log):
                log("[*] 已进入验证码页")
                return True

            loading = is_email_submit_loading()
            now = time.time()
            if now - last_log >= 8:
                log(
                    f"[*] 等待验证码页... loading={loading} api={api_status} "
                    f"left={int(deadline - now)}s"
                )
                last_log = now

            if loading:
                time.sleep(0.5)
                continue

            hints = page_error_hints(log=log)
            # 过滤 cookie 文案误报
            hints = [
                h
                for h in hints
                if not re.search(r"cookie|隐私|privacy|terms", h or "", re.I)
            ]
            if hints:
                log(f"[!] 页面提示: {hints[:2]} api={api_status}")
                last_err = str(hints[:2])
                break  # 跳出等待，进入下一 round 重试

            # 无 loading 无错误无验证码：稍等再判
            time.sleep(0.8)

        # round 失败，下一轮重开
        try:
            d.dump_debug(f"fill_email_round{round_i}_fail")
        except Exception:
            pass

    dbg = ""
    try:
        dbg = d.dump_debug("fill_email_no_code_page")
    except Exception as e:
        dbg = f"dump_err={e}"
    raise Exception(
        f"邮箱提交后未进入验证码页 email={email} last={last_err} url={get_current_url()} {dbg}"
    )



def fill_code_and_submit(code, timeout=60, log=print):
    d = get_driver()
    # 页面 name=code 通常 maxlength=6，无连字符
    clean_code = str(code).replace("-", "").replace(" ", "").strip().upper()
    display_code = code if "-" in str(code) else (
        f"{clean_code[:3]}-{clean_code[3:]}" if len(clean_code) == 6 else clean_code
    )
    if not clean_code:
        raise Exception("验证码为空")

    if not wait_code_input(timeout=min(20, timeout), log=log):
        log("[!] 验证码框未就绪，仍尝试填写...")

    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        _dismiss_cookie_banner(log=log)
        filled_ok = False

        # 1) Playwright 单框 name=code
        for sel in (
            'input[name="code"]',
            'input[data-input-otp="true"]',
            'input[autocomplete="one-time-code"]',
            'input[data-testid="code"]',
        ):
            try:
                loc = d.page.locator(sel).first
                loc.wait_for(state="visible", timeout=1500)
                if not loc.is_visible():
                    continue
                loc.click(timeout=3000)
                try:
                    loc.fill("")
                except Exception:
                    pass
                loc.fill(clean_code, timeout=8000)
                got = ""
                try:
                    got = (loc.input_value(timeout=2000) or "").replace("-", "").replace(" ", "").upper()
                except Exception:
                    pass
                if got == clean_code or clean_code in got:
                    filled_ok = True
                    log(f"[*] 验证码已填入({sel}): {display_code}")
                    break
                # 慢速
                loc.click()
                loc.fill("")
                loc.press_sequentially(clean_code, delay=40)
                got = (loc.input_value(timeout=2000) or "").replace("-", "").replace(" ", "").upper()
                if got == clean_code or clean_code in got:
                    filled_ok = True
                    log(f"[*] 验证码慢速填入: {display_code}")
                    break
            except Exception as e:
                last_err = str(e)
                continue

        # 2) 多框 OTP
        if not filled_ok:
            try:
                boxes = d.page.locator('input[maxlength="1"]')
                n = boxes.count()
                if n >= len(clean_code):
                    for i, ch in enumerate(clean_code):
                        boxes.nth(i).click(timeout=2000)
                        boxes.nth(i).fill(ch, timeout=2000)
                    filled_ok = True
                    log(f"[*] 验证码多框填入: {display_code}")
            except Exception as e:
                last_err = str(e)

        # 3) JS 兜底
        if not filled_ok:
            try:
                raw = d.page.evaluate(
                    """(code) => {
function isVisible(node) {
  if (!node) return false;
  const s = getComputedStyle(node);
  if (s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
  const r = node.getBoundingClientRect();
  return r.width>0 && r.height>0;
}
function setVal(input, value) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  const tracker = input._valueTracker;
  if (tracker) tracker.setValue('');
  if (setter) setter.call(input, value); else input.value = value;
  input.dispatchEvent(new InputEvent('input', {bubbles:true, data:value, inputType:'insertText'}));
  input.dispatchEvent(new Event('change', {bubbles:true}));
}
const agg = Array.from(document.querySelectorAll(
  'input[name="code"], input[data-input-otp="true"], input[autocomplete="one-time-code"]'
)).find(n => isVisible(n) && !n.disabled && Number(n.maxLength||6) > 1);
if (agg) { agg.focus(); setVal(agg, code); return {ok: !!(agg.value||'').trim(), via:'agg', v:agg.value}; }
const boxes = Array.from(document.querySelectorAll('input')).filter(n =>
  isVisible(n) && !n.disabled && Number(n.maxLength||0)===1);
if (boxes.length >= code.length) {
  for (let i=0;i<code.length;i++){ boxes[i].focus(); setVal(boxes[i], code[i]); }
  return {ok:true, via:'boxes'};
}
return {ok:false, via:'none'};
}""",
                    clean_code,
                )
                if isinstance(raw, dict) and raw.get("ok"):
                    filled_ok = True
                    log(f"[*] 验证码 JS 填入: {display_code} ({raw.get('via')})")
            except Exception as e:
                last_err = str(e)

        if not filled_ok:
            time.sleep(0.5)
            continue

        time.sleep(0.4)
        # 点 Confirm email / 确认邮箱
        clicked = False
        for text in (
            "Confirm email",
            "确认邮箱",
            "继续",
            "下一步",
            "Continue",
            "Next",
            "Confirm",
            "Verify",
        ):
            try:
                loc = d.page.get_by_role("button", name=re.compile(re.escape(text), re.I))
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=5000)
                    log(f"[*] 已提交验证码: 点击「{text}」")
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            try:
                d.page.locator('form button[type="submit"], button[type="submit"]').first.click(timeout=4000)
                log("[*] 已提交验证码: submit")
                clicked = True
            except Exception:
                try:
                    d.page.keyboard.press("Enter")
                    log("[*] 已提交验证码: Enter")
                    clicked = True
                except Exception as e:
                    last_err = str(e)

        if clicked:
            time.sleep(1.5)
            return True
        time.sleep(0.5)

    dbg = ""
    try:
        dbg = d.dump_debug("fill_code_fail")
    except Exception as e:
        dbg = f"dump_err={e}"
    raise Exception(f"验证码填写/提交失败 code={display_code} last={last_err} {dbg}")


def _sync_turnstile_token(token: str):
    if not token:
        return
    eval_js(_embed_js(r"""(()=>{
const token = %%token%%;
const input = document.querySelector('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]');
if (input) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
    || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
  if (setter) setter.call(input, token); else input.value = token;
  input.dispatchEvent(new Event('input', {bubbles:true}));
  input.dispatchEvent(new Event('change', {bubbles:true}));
}
return 'ok';
})()""", token=token))


def fill_profile_and_submit(timeout=120, log=print, handle_turnstile: bool = False):
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled = False
    wait_cf_since = None
    last_cf_log = 0.0
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
return 'filled';
})()""", given_name=given_name, family_name=family_name, password=password)
            filled = eval_js(js)
            if filled in ('not-ready', 'fill-failed'):
                time.sleep(0.8)
                continue
            form_filled = True
            log(f'[*] 资料已填写: {given_name} {family_name}')
        if handle_turnstile:
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
            if isinstance(cf_status, str) and cf_status.startswith('solved:'):
                token = cf_status.split(':', 1)[1]
                _sync_turnstile_token(token)
                log(f'[*] Turnstile 验证已通过，Token 长度={len(token)}')
            elif isinstance(cf_status, str) and cf_status.startswith('waiting:'):
                token_len = cf_status.split(':', 1)[1]
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                cf_waited = now - wait_cf_since
                if now - last_cf_log >= 5:
                    log(f'[*] 等待 Turnstile... Token长度={token_len} (已等 {cf_waited:.0f}s)')
                    last_cf_log = now
                if cf_waited >= timeout:
                    raise Exception(f'Turnstile 在 {timeout}s 内未通过')
                time.sleep(1)
                continue
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
        if submit_state == 'submitted':
            log(f'[*] 已提交注册资料: {given_name} {family_name}')
            return {'given_name': given_name, 'family_name': family_name, 'password': password}
        if submit_state == 'form-gone':
            log(f'[*] 注册表单已消失（视为已提交）: {given_name} {family_name}')
            return {'given_name': given_name, 'family_name': family_name, 'password': password}
        time.sleep(0.5)
    if wait_cf_since is not None:
        raise Exception(f'注册资料已填写，但 Turnstile 在 {timeout}s 内未通过')
    raise Exception('注册资料填写/提交失败')


def _try_document_cookie_sso() -> str | None:
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
    val = (raw or '').strip()
    if len(val) > 20:
        return val
    try:
        for c in get_driver().cookies('https://grok.com'):
            if c.get('name') in ('sso', 'sso-rw'):
                v = str(c.get('value') or '')
                if len(v) > 20:
                    return v
        for c in get_driver().cookies('https://accounts.x.ai'):
            if c.get('name') in ('sso', 'sso-rw'):
                v = str(c.get('value') or '')
                if len(v) > 20:
                    return v
    except Exception:
        pass
    return None


def _click_by_id(btn_id: str, log=print):
    if not btn_id:
        return
    try:
        get_driver().click(f'#{btn_id}', timeout=5000)
    except Exception as e:
        try:
            eval_js(_embed_js(r"""(()=>{ const el=document.getElementById(%%id%%); if(el){el.click(); return 'ok';} return 'miss'; })()""", id=btn_id))
        except Exception as e2:
            log(f'[!] 点击失败 id={btn_id}: {e} / {e2}')


# ─── OIDC / CPA / NSFW / 编排 ────────────────────────────────────────────────
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



def discover_oidc_endpoints(log=print) -> dict:
    disc_url = 'https://auth.x.ai/.well-known/openid-configuration'
    try:
        proxies = _get_proxies()
        import requests
        r = requests.get(
            disc_url,
            headers={'Accept': 'application/json', 'User-Agent': 'grok-register-cpa/1.0'},
            proxies=proxies or None,
            timeout=10,
        )
        data = r.json()
        dev_ep = data.get('device_authorization_endpoint')
        tok_ep = data.get('token_endpoint')
        if dev_ep and tok_ep:
            log(f'[*] OIDC 服务发现成功: device_ep={dev_ep}, token_ep={tok_ep}')
            return {'device_authorization_endpoint': dev_ep, 'token_endpoint': tok_ep}
    except Exception as e:
        log(f'[!] OIDC 服务发现异常 ({e})，将降级使用静态 Endpoint')
    return {
        'device_authorization_endpoint': 'https://auth.x.ai/oauth2/device/code',
        'token_endpoint': 'https://auth.x.ai/oauth2/token',
    }


def request_device_code(device_endpoint: str | None = None) -> dict:
    import requests
    url = device_endpoint or 'https://auth.x.ai/oauth2/device/code'
    proxies = _get_proxies()
    if not proxies:
        raise RuntimeError('当前未绑定代理，拒绝直连 device code')
    r = requests.post(
        url,
        data={
            'client_id': CLIENT_ID,
            'scope': 'openid profile email offline_access grok-cli:access api:access',
        },
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
            'User-Agent': 'grok-register-cpa/1.0',
        },
        proxies=proxies,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def poll_device_token(device_code: str, interval: int = 5, timeout: int = 120, token_endpoint: str | None = None, log=print) -> dict:
    import requests
    url = token_endpoint or 'https://auth.x.ai/oauth2/token'
    proxies = _get_proxies()
    if not proxies:
        raise RuntimeError('当前未绑定代理，拒绝直连 token poll')
    deadline = time.time() + timeout
    payload = {
        'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
        'device_code': device_code.strip(),
        'client_id': CLIENT_ID,
    }
    curr_interval = interval
    net_streak = 0
    while time.time() < deadline:
        try:
            r = requests.post(
                url,
                data=payload,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Accept': 'application/json',
                    'User-Agent': 'grok-register-cpa/1.0',
                },
                proxies=proxies,
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                if 'access_token' in data:
                    return data
            try:
                err_body = r.json()
            except Exception:
                err_body = {}
            err = err_body.get('error', '')
            net_streak = 0
            if err == 'authorization_pending':
                time.sleep(curr_interval)
                continue
            if err == 'slow_down':
                curr_interval = min(curr_interval + 5, 30)
                log(f'[*] 触发 slow_down 调整轮询间隔为 {curr_interval}s')
                time.sleep(curr_interval)
                continue
            if err in ('expired_token', 'access_denied', 'invalid_grant'):
                log(f'[!] 设备码失效/拒绝: {err}')
                return {'error': err, 'details': err_body}
            time.sleep(curr_interval)
        except Exception as e:
            net_streak += 1
            if net_streak <= 20:
                time.sleep(curr_interval)
            else:
                log(f'[!] 轮询遭遇持续网络故障: {e}')
                break
    return {}


def _generate_birthdate():
    import datetime as dt
    today = dt.date.today()
    age = random.randint(20, 40)
    return f'{today.year - age}-{random.randint(1,12):02d}-{random.randint(1,28):02d}T16:00:00.000Z'


def enable_nsfw_for_token(token, log=print):
    from curl_cffi import requests as creq
    proxies = _get_proxies()
    ua = _get_user_agent()
    try:
        with creq.Session(impersonate='chrome120', proxies=proxies or None) as session:
            session.headers.update({
                'user-agent': ua,
                'cookie': f'sso={token}; sso-rw={token}',
                'authorization': f'Bearer {token}',
            })
            url = 'https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion'
            payload = struct.pack('B', (2 << 3) | 0) + struct.pack('B', 1)
            data = b'\x00' + struct.pack('>I', len(payload)) + payload
            res = session.post(url, data=data, headers={
                'content-type': 'application/grpc-web+proto',
                'x-grpc-web': '1',
                'x-user-agent': 'connect-es/2.1.1',
                'origin': 'https://accounts.x.ai',
                'referer': 'https://accounts.x.ai/accept-tos',
            }, timeout=15)
            log(f'[Debug] set_tos status: {res.status_code}')
            if not (200 <= res.status_code < 300):
                return False, f'set_tos HTTP {res.status_code}'
            res = session.post(
                'https://grok.com/rest/auth/set-birth-date',
                json={'birthDate': _generate_birthdate()},
                headers={'content-type': 'application/json', 'origin': 'https://grok.com', 'referer': 'https://grok.com/'},
                timeout=15,
            )
            log(f'[Debug] set_birth_date status: {res.status_code}')
            if not (200 <= res.status_code < 300):
                return False, f'set_birth_date HTTP {res.status_code}'
            field1_content = bytes([0x10, 0x01])
            field1 = bytes([0x0A, len(field1_content)]) + field1_content
            nsfw_string = b'always_show_nsfw_content'
            field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
            field2 = bytes([0x12, len(field2_inner)]) + field2_inner
            nsfw_payload = field1 + field2
            nsfw_data = b'\x00' + struct.pack('>I', len(nsfw_payload)) + nsfw_payload
            res = session.post(
                'https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls',
                data=nsfw_data,
                headers={
                    'content-type': 'application/grpc-web+proto',
                    'x-grpc-web': '1',
                    'origin': 'https://grok.com',
                    'referer': 'https://grok.com/',
                },
                timeout=15,
            )
            log(f'[Debug] update_nsfw status: {res.status_code}')
            if not (200 <= res.status_code < 300):
                return False, f'update_nsfw HTTP {res.status_code}'
            return True, '成功开启 NSFW'
    except Exception as e:
        return False, f'异常: {e}'


def append_account_line(path, email, password, sso):
    with open(path, 'a', encoding='utf-8') as f:
        f.write(f"{email}----{password}----{sso}\n")
        f.flush()
        os.fsync(f.fileno())


def save_mail_credential(base_dir, email, credential):
    path = os.path.join(base_dir, 'mail_credentials.txt')
    with open(path, 'a', encoding='utf-8') as f:
        f.write(f"{email}\t{credential}\n")
        f.flush()
        os.fsync(f.fileno())


def authorize_grok_build(email: str, password: str, log=print, timeout=120) -> str:
    d = get_driver()
    log('[*] 1. 动态获取 OIDC Endpoint 配置...')
    endpoints = discover_oidc_endpoints(log=log)
    device_ep = endpoints.get('device_authorization_endpoint')
    token_ep = endpoints.get('token_endpoint')

    log('[*] 等待资料提交后登录跳转完成 (到达 grok.com)...')
    wait_deadline = time.time() + 30
    while time.time() < wait_deadline:
        curr = get_current_url()
        if 'grok.com' in curr:
            log('[*] 登录跳转完成，当前处于 grok.com')
            break
        time.sleep(1)
    else:
        log('[!] 提示：未在 30 秒内检测到跳转至 grok.com，直接尝试授权导航...')

    log('[*] 2. 授权前准备：提取 sso Cookie 并跨域注入...')
    sso_val = _try_document_cookie_sso()
    if not sso_val:
        log('[!] 未能在当前环境提取到 sso，后续可能需要重新登录')
    else:
        log(f'[*] 成功提取 sso: {sso_val[:15]}...')

    try:
        d.goto('https://accounts.x.ai/account', timeout=30000)
        wait_doc_loaded(10)
        if sso_val:
            log('[*] 正在向 accounts.x.ai 注入跨域 Session Cookie...')
            try:
                d.context.add_cookies([
                    {'name': 'sso', 'value': sso_val, 'domain': '.x.ai', 'path': '/', 'secure': True, 'sameSite': 'None'},
                    {'name': 'sso-rw', 'value': sso_val, 'domain': '.x.ai', 'path': '/', 'secure': True, 'sameSite': 'None'},
                    {'name': 'sso', 'value': sso_val, 'domain': 'accounts.x.ai', 'path': '/', 'secure': True, 'sameSite': 'None'},
                    {'name': 'sso-rw', 'value': sso_val, 'domain': 'accounts.x.ai', 'path': '/', 'secure': True, 'sameSite': 'None'},
                    {'name': 'sso', 'value': sso_val, 'domain': 'auth.x.ai', 'path': '/', 'secure': True, 'sameSite': 'None'},
                    {'name': 'sso-rw', 'value': sso_val, 'domain': 'auth.x.ai', 'path': '/', 'secure': True, 'sameSite': 'None'},
                ])
            except Exception as e:
                log(f'[!] context.add_cookies 失败，改 JS 注入: {e}')
                eval_js(f"""(() => {{
                document.cookie = "sso={sso_val}; domain=.x.ai; path=/; secure; samesite=none";
                document.cookie = "sso-rw={sso_val}; domain=.x.ai; path=/; secure; samesite=none";
                return 'ok';
            }})()""")
            time.sleep(1.0)
            d.goto('https://accounts.x.ai/account', timeout=30000)
            wait_doc_loaded(10)
        time.sleep(2.0)
    except Exception as e:
        log(f'[!] 导航至 accounts.x.ai 出现异常: {e}')

    log('[*] 3. 向 auth.x.ai 申请 Device Code...')
    dev_info = request_device_code(device_endpoint=device_ep)
    device_code = dev_info.get('device_code', '')
    user_code = dev_info.get('user_code', '')
    complete_url = dev_info.get('verification_uri_complete', '') or f'https://accounts.x.ai/oauth2/device?user_code={user_code}'
    interval = int(dev_info.get('interval', 5))
    if not device_code or not complete_url:
        raise Exception('获取 Device Code 失败')

    log(f'[*] 成功获取 User Code: {user_code}')
    log(f'[*] 导航访问授权页面: {complete_url}')
    d.goto(complete_url, timeout=30000)
    wait_doc_loaded(10)

    email_js = json.dumps(email)
    password_js = json.dumps(password)
    user_code_js = json.dumps(user_code)
    deadline = time.time() + timeout
    log('[*] 执行多阶段 JS 自动化进行设备确认与授权...')
    while time.time() < deadline:
        res = eval_js(f"""(() => {{
            const text = document.body ? document.body.innerText : '';
            const url = window.location.href;
            const ensureId = (el) => {{
                if (!el.id) el.id = '__grok_auth_btn_' + Date.now();
                return el.id;
            }};
            if (url.includes('/device/done') || text.includes('设备已授权') || text.includes('Device Authorized') || text.includes('成功连接')) {{
                return 'device_authorized';
            }}
            if (url.includes('/sign-in') || url.includes('login')) {{
                const buttons = Array.from(document.querySelectorAll('button'));
                const emailBtn = buttons.find(b => {{
                    const t = (b.innerText || '').trim();
                    return t === '使用邮箱登录' || t.includes('Continue with email') || t.includes('Sign in with email');
                }});
                if (emailBtn) return 'found_signin_email_btn:' + ensureId(emailBtn);
                const emailInput = document.querySelector('input[type="email"], input[name="email"]');
                const passwordInput = document.querySelector('input[type="password"], input[name="password"]');
                if (emailInput && !passwordInput) {{
                    if (!emailInput.value) {{
                        emailInput.value = {email_js};
                        emailInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                    const nextBtn = buttons.find(b => {{
                        const t = (b.innerText || '').trim();
                        return t === '下一步' || t === 'Next' || t === 'Continue' || t === '继续';
                    }});
                    if (nextBtn) return 'found_signin_next_btn:' + ensureId(nextBtn);
                }}
                if (emailInput && passwordInput) {{
                    if (!emailInput.value) {{
                        emailInput.value = {email_js};
                        emailInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                    if (!passwordInput.value) {{
                        passwordInput.value = {password_js};
                        passwordInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                    const loginBtn = buttons.find(b => {{
                        const t = (b.innerText || '').trim();
                        return t === '登录' || t === 'Sign in' || t === 'Log in';
                    }});
                    if (loginBtn) return 'found_signin_login_btn:' + ensureId(loginBtn);
                }}
                return 'waiting_signin';
            }}
            const codeInput = document.querySelector('input[name=user_code]');
            if (codeInput && !url.includes('/consent')) {{
                if (!codeInput.value) {{
                    codeInput.value = {user_code_js};
                    codeInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
                const buttons = Array.from(document.querySelectorAll('button, input[type=submit]'));
                const nextBtn = buttons.find(b => {{
                    const t = (b.innerText || b.value || '').trim();
                    return t === '继续' || t === 'Continue' || t === 'Next' || t === '确认';
                }}) || document.querySelector('button[type=submit]');
                if (nextBtn) return 'found_device_next:' + ensureId(nextBtn);
            }}
            if (url.includes('/consent') || text.includes('授权 Grok Build') || text.includes('Authorize Grok Build') || text.includes('Grok Build')) {{
                const buttons = Array.from(document.querySelectorAll('button, a, input[type=submit]'));
                const allowBtn = buttons.find(b => {{
                    const t = (b.innerText || b.value || '').trim();
                    return t === '允许' || t === 'Allow' || t === 'Authorize' || t === 'Approve';
                }});
                if (allowBtn) return 'found_allow:' + ensureId(allowBtn);
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
        if res_str == 'device_authorized':
            log('[*] 前端已确认【设备已授权】...')
            break
        if res_str.startswith('found_signin_email_btn:'):
            _click_by_id(res_str.split(':', 1)[1], log=log)
            time.sleep(2.0)
        elif res_str.startswith('found_signin_next_btn:'):
            _click_by_id(res_str.split(':', 1)[1], log=log)
            time.sleep(2.0)
        elif res_str.startswith('found_signin_login_btn:'):
            _click_by_id(res_str.split(':', 1)[1], log=log)
            time.sleep(4.0)
        elif res_str.startswith('found_allow:'):
            _click_by_id(res_str.split(':', 1)[1], log=log)
            time.sleep(2.0)
        elif res_str == 'submitted_form_fallback':
            log('[*] Form Submit 兜底已触发')
            time.sleep(2.0)
        elif res_str.startswith('found_device_next:'):
            _click_by_id(res_str.split(':', 1)[1], log=log)
            time.sleep(2.5)
        else:
            time.sleep(1.5)

    log('[*] 开始轮询 access token...')
    token_result = poll_device_token(device_code, interval=interval, timeout=60, token_endpoint=token_ep, log=log)
    if token_result and token_result.get('access_token'):
        auth_dir = str(cfg('cpa_auth_dir', 'cpa_auths') or 'cpa_auths')
        if not os.path.isabs(auth_dir):
            auth_dir = str(ROOT / auth_dir)
        hotload = None
        if cfg_bool('cpa_copy_to_hotload', False):
            hotload = str(cfg('cpa_hotload_dir', '') or '') or None
        save_cpa_json_credential(token_result, token_endpoint=token_ep, auth_dir=auth_dir, hotload_dir=hotload, log=log)
        return token_result.get('access_token')
    raise Exception('等待超时或授权失败：未能获取 Access Token')


def register_one_account(log=print, enable_nsfw=True, provider=None, handle_turnstile=False):
    # 先拿邮箱，避免页面空等导致会话/横幅变化
    log('[*] 1. 创建临时邮箱')
    email, dev_token = get_email_and_token(provider=provider)
    log(f'[*] 邮箱: {email}')
    save_mail_credential(str(ROOT), email, dev_token)

    log('[*] 2. 打开注册页并填写邮箱')
    open_signup_page(log=log)
    fill_email_and_submit(email, log=log)
    # 提交后应已在验证码页；再兜底等一次
    if not wait_code_input(timeout=15, log=log):
        log('[!] 提交后未立刻看到验证码框，继续拉邮件（可能仍在加载）')
    log('[*] 3. 拉取验证码')
    code = get_oai_code(dev_token, email, provider=provider, log=log)
    log(f'[*] 验证码: {code}')
    log('[*] 3b. 填写验证码')
    fill_code_and_submit(code, log=log)
    log('[*] 4. 填写资料')
    profile = fill_profile_and_submit(log=log, handle_turnstile=handle_turnstile)
    log(f"[*] 资料: {profile['given_name']} {profile['family_name']}")
    log('[*] 5. 授权 Grok Build')
    token = authorize_grok_build(email=email, password=profile['password'], log=log)
    log(f'[*] 授权成功, Access Token 长度: {len(token)}')
    if enable_nsfw:
        log('[*] 6. 开启 NSFW')
        try:
            ok, msg = enable_nsfw_for_token(token, log=log)
            log(('[' + ('+' if ok else '!') + f'] NSFW: {msg}'))
        except Exception as e:
            log(f'[!] 开启 NSFW 出现异常: {e}')
    return email, profile['password'], token


def run_batch(count: int, ports: list[int], headless: bool, enable_nsfw: bool, provider: str | None, handle_turnstile: bool, executable_path: str, log=print):
    global _driver
    candidates = list_candidate_ports(ports, log=log)
    if not candidates:
        log('[!] 没有可用代理端口（全在冷却或探活失败），退出')
        return 0
    out_path = ROOT / ('accounts_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.txt')
    success = 0
    attempted = 0
    for port in candidates:
        if success >= count:
            break
        attempted += 1
        proxy = proxy_url_for_port(port)
        log('=' * 60)
        log(f'[*] 开始第 {success+1}/{count} 个账号 | 代理 {proxy} | 端口 {port}')
        set_current_proxy(proxy)
        _driver = None
        port_marked = False
        try:
            with CamoufoxDriver(proxy_url=proxy, headless=headless, executable_path=executable_path) as driver:
                _driver = driver
                # 浏览器真正拉起后再记冷却，避免邮箱/启动失败白烧端口
                mark_port_used(port)
                port_marked = True
                log(f'[*] 已写入冷却 {cooldown_sec()}s: port={port}')
                email, password, token = register_one_account(
                    log=log, enable_nsfw=enable_nsfw, provider=provider, handle_turnstile=handle_turnstile,
                )
                append_account_line(str(out_path), email, password, token)
                success += 1
                log(f'[+] 成功 {success}/{count}: {email}')
        except Exception as e:
            import traceback
            log(f'[-] 端口 {port} 注册失败: {e}')
            log(traceback.format_exc())
            if not port_marked:
                log(f'[*] 浏览器未成功启动，端口 {port} 不记冷却')
            try:
                if _driver is not None:
                    log('[*] debug: ' + _driver.dump_debug(f'fail_port_{port}'))
            except Exception:
                pass
        finally:
            _driver = None
            set_current_proxy('')
    log('=' * 60)
    log(f'[*] 完成: 成功 {success}/{count}，尝试 {attempted} 个代理，产物: ' + (str(out_path) if success else '(无)'))
    return success


def parse_args():
    p = argparse.ArgumentParser(description='Grok 注册机 - Camoufox + 代理池半小时冷却')
    p.add_argument('-n', '--count', type=int, default=None, help='注册账号数量，默认 1')
    p.add_argument('--ports', type=str, default='', help='代理端口，如 1801-1850 或 1801,1802')
    p.add_argument('--cooldown', type=int, default=None, help='冷却秒数，默认 1800')
    p.add_argument('--headless', action='store_true', help='无头模式')
    p.add_argument('--no-nsfw', action='store_true', help='跳过 NSFW')
    p.add_argument('--provider', type=str, default=None, help='邮箱 provider')
    p.add_argument('--turnstile', dest='turnstile', action='store_true', help='启用 Turnstile 等待')
    p.add_argument('--no-turnstile', dest='turnstile', action='store_false', help='跳过 Turnstile 等待')
    p.set_defaults(turnstile=None)
    p.add_argument('--camoufox-path', type=str, default='', help='camoufox.exe 路径')
    p.add_argument('--dry-run', action='store_true', help='只打印端口冷却状态')
    return p.parse_args()


def main():
    load_config()
    args = parse_args()
    if args.cooldown is not None:
        _config['ip_cooldown_sec'] = int(args.cooldown)
    ports = parse_ports_spec(args.ports)
    count = args.count if args.count is not None else int(cfg('register_count', 1) or 1)
    count = max(1, int(count))
    headless = bool(args.headless or cfg_bool('camoufox_headless', False))
    enable_nsfw = (not args.no_nsfw) and cfg_bool('enable_nsfw', True)
    provider = args.provider or str(cfg('email_provider', 'yyds') or 'yyds')
    if args.turnstile is None:
        handle_turnstile = cfg_bool('handle_turnstile', False)
    else:
        handle_turnstile = bool(args.turnstile)
    exe = args.camoufox_path or str(cfg('camoufox_path', '') or '')
    if not exe:
        try:
            from camoufox.pkgman import launch_path
            exe = str(launch_path())
        except Exception:
            exe = DEFAULT_CAMOUFOX_PATH if Path(DEFAULT_CAMOUFOX_PATH).is_file() else ''
    print(f'[*] ports={ports[0]}-{ports[-1]} ({len(ports)}) count={count} cooldown={cooldown_sec()}s')
    print(f'[*] provider={provider} nsfw={enable_nsfw} turnstile={handle_turnstile} headless={headless}')
    print('[*] camoufox=' + (exe or '(package default)'))
    if args.dry_run:
        data = load_cooldown()
        print('[dry-run] 端口状态:')
        for p in ports:
            left = remaining_cooldown(p, data)
            alive = probe_proxy_port(p)
            if left > 0:
                st = f'cooling {int(left)}s'
            elif not alive:
                st = 'dead'
            else:
                st = 'ready'
            print(f'  {p}: {st}')
        list_candidate_ports(ports)
        return
    run_batch(
        count=count,
        ports=ports,
        headless=headless,
        enable_nsfw=enable_nsfw,
        provider=provider,
        handle_turnstile=handle_turnstile,
        executable_path=exe,
    )


if __name__ == '__main__':
    main()
