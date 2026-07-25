#!/usr/bin/env python3
"""一次性探针：跑注册流程到第 4 步资料页，dump Turnstile 真实 DOM 结构后停止（不关 tab）。"""
import time

import grok_register_opencli as g

PROBE_JS = r"""(()=>{
  const out = {};
  out.url = location.href;
  out.iframes = Array.from(document.querySelectorAll('iframe')).map(f=>{
    const r = f.getBoundingClientRect();
    return {src: String(f.src||'').slice(0,120), w: Math.round(r.width), h: Math.round(r.height), display: getComputedStyle(f).display};
  });
  out.shadowHosts = [];
  const walk = (root, depth) => {
    if (depth > 6) return;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        out.shadowHosts.push(el.tagName + ' ' + String(el.className||'').slice(0,60));
        walk(el.shadowRoot, depth+1);
      }
    }
  };
  walk(document, 0);
  const inp = document.querySelector('input[name="cf-turnstile-response"]');
  out.inputInfo = inp ? {
    parentTag: inp.parentElement ? inp.parentElement.tagName + '.' + String(inp.parentElement.className||'') : null,
    parentHTML: inp.parentElement ? inp.parentElement.outerHTML.slice(0, 800) : null,
  } : null;
  const cfd = document.querySelector('.cf-turnstile, [data-sitekey]');
  out.cfDiv = cfd ? cfd.outerHTML.slice(0, 800) : null;
  out.scripts = Array.from(document.querySelectorAll('script[src]')).map(s=>s.src).filter(s=>/turnstile|challenge|cloudflare/i.test(s)).slice(0,5);
  try { out.turnstileKeys = Object.getOwnPropertyNames(window.turnstile || {}); } catch(e) { out.turnstileKeys = 'err:' + e; }
  out.patchMarkers = ['turnstilePatch','__turnstilePatch','cfTurnstilePatch','dtp'].map(k=>k+':'+(typeof window[k]));
  return JSON.stringify(out);
})()"""


def main():
    g.load_config()
    g.ensure_browser_bridge()
    print("[probe] 打开注册页")
    g.open_signup_page()
    print("[probe] 创建邮箱")
    email, dev_token = g.get_email_and_token(provider="yyds")
    print("[probe] email:", email)
    g.fill_email_and_submit(email)
    print("[probe] 等验证码")
    code = g.get_oai_code(dev_token, email, provider="yyds", log=print)
    print("[probe] code:", code)
    g.fill_code_and_submit(code)
    print("[probe] 等资料页")
    for _ in range(30):
        st = g.eval_js(r"""(()=>{
            const p = document.querySelector('input[type="password"], input[data-testid="password"], input[name="password"]');
            return p ? 'ready' : 'wait';
        })()""")
        if st == "ready":
            break
        time.sleep(1)
    print("[probe] 资料页就绪，等 5s 让 Turnstile 渲染")
    time.sleep(5)
    print("[probe] ==== DOM dump ====")
    print(g.eval_js(PROBE_JS))
    print("[probe] ==== frames ====")
    try:
        print(g.run("frames", timeout=10))
    except Exception as e:
        print("[probe] frames 失败:", e)
    try:
        g.run("screenshot", r"E:\ai\grok-register\probe_widget.png", timeout=15)
        print("[probe] 截图已存 probe_widget.png")
    except Exception as e:
        print("[probe] 截图失败:", e)
    print("[probe] 完成（tab 保持打开）")


if __name__ == "__main__":
    main()
