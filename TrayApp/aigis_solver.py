from __future__ import annotations

import base64
import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, Thread
import urllib.parse
from typing import Any

try:
    from app_config import HOME
    ASSETS = HOME / "assets"
except Exception:
    ASSETS = Path(__file__).resolve().parent / "assets"

GT4_PATH = ASSETS / "gt4.js"
GT4_URLS = [
    "https://static.geetest.com/v4/gt4.js",
    "https://static.geetest.com/v4/gt4.js?v=1",
]

GT4_HOSTS = {"static.geetest.com"}  # 允许下载验证组件的主机白名单

GT4_CACHE = ""


def parse_aigis_header(header_val: str) -> dict[str, Any]:
    if not header_val:
        return {}
    try:
        obj = json.loads(header_val)
    except Exception:
        return {}
    data = obj.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "session_id": obj.get("session_id") or "",
        "mmt_type": obj.get("mmt_type"),
        "gt": data.get("gt") or "",
        "risk_type": data.get("risk_type") or "icon",
        "new_captcha": data.get("new_captcha", 1),
        "use_v4": data.get("use_v4", True),
        "raw": header_val,
    }


def build_aigis_header(session_id: str, captcha_id: str, validate: dict) -> str:
    payload = dict(validate or {})
    payload["captcha_id"] = captcha_id
    payload["userInfo"] = {"session_id": session_id}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"{session_id};{base64.b64encode(raw.encode('utf-8')).decode('ascii')}"


def _load_gt4_js() -> str:
    global GT4_CACHE
    if GT4_CACHE:
        return GT4_CACHE
    if GT4_PATH.exists():
        try:
            GT4_CACHE = GT4_PATH.read_text(encoding="utf-8")
            return GT4_CACHE
        except Exception:
            pass
    try:
        import httpx

        for url in GT4_URLS:
            try:
                host = urllib.parse.urlparse(url).hostname or ""
                if urllib.parse.urlparse(url).scheme != "https" or host not in GT4_HOSTS:
                    continue  # 仅允许白名单主机，避免被替换成任意地址
                r = httpx.get(url, timeout=20, follow_redirects=True)
                if r.status_code == 200 and len(r.content) > 1000:
                    text = r.text
                    try:
                        ASSETS.mkdir(parents=True, exist_ok=True)
                        GT4_PATH.write_text(text, encoding="utf-8")
                    except Exception:
                        pass
                    GT4_CACHE = text
                    return text
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _html_for(gt: str, session_id: str, risk_type: str, port: int | None = None) -> str:
    gt4 = _load_gt4_js()
    if gt4:
        script_block = "<script>\n" + gt4 + "\n</script>"
        script_note = "已内嵌本地 gt4.js"
    else:
        urls = "".join(
            f'<script src="{u}" onerror="window.__gtFail=(window.__gtFail||0)+1"></script>'
            for u in GT4_URLS
        )
        script_block = urls
        script_note = "使用在线 gt4.js"

    post_js = ""
    finish_target = "pywebview"
    if port:
        finish_target = "http"
        post_js = f"""
    fetch('http://127.0.0.1:{port}/done', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(out)
    }}).then(function() {{
      document.body.innerHTML = '<h2>验证成功</h2><p>可以关闭本页，返回应用。</p>';
    }}).catch(function(e) {{ st('回传失败：' + e); }});
"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>图形验证</title>
<style>
  body {{ font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; margin: 0; padding: 20px;
         background: #F3F4F6; color: #1F2937; }}
  h2 {{ font-size: 15px; margin: 0 0 6px; }}
  p {{ font-size: 12px; color: #6B7280; margin: 0 0 12px; }}
  #status {{ margin-top: 12px; font-size: 12px; color: #2563EB; min-height: 36px; white-space: pre-wrap; }}
  #box {{ background: #fff; border: 1px solid #E5E7EB; border-radius: 10px; padding: 14px; }}
</style>
{script_block}
</head>
<body>
  <div id="box">
    <h2>请完成图形验证</h2>
    <p>脚本源：{script_note} · 通过后自动返回</p>
    <div id="captcha"></div>
    <div id="status">正在检查极验脚本…</div>
  </div>
<script>
(function () {{
  var GT = {json.dumps(gt)};
  var SESSION = {json.dumps(session_id)};
  var RISK = {json.dumps(risk_type)};
  var mode = {json.dumps(finish_target)};
  function st(t) {{ document.getElementById('status').textContent = t; }}
  window.onerror = function (m) {{ st('脚本错误：' + m); }};

  function pack(raw) {{
    var v = raw || {{}};
    return {{
      lot_number: v.lot_number || v.lotNumber || '',
      captcha_output: v.captcha_output || v.captchaOutput || '',
      pass_token: v.pass_token || v.passToken || '',
      gen_time: v.gen_time || v.genTime || '',
      _captcha_id: GT,
      _session_id: SESSION
    }};
  }}

  function finish(raw) {{
    var out = pack(raw);
    st('验证成功，正在返回…');
    try {{
      if (window.pywebview && window.pywebview.api && window.pywebview.api.on_success) {{
        window.pywebview.api.on_success(out);
        return;
      }}
    }} catch (e) {{}}
    {post_js}
  }}

  function start() {{
    var hasInit = typeof initGeetest4 === 'function';
    var hasCtor = typeof Geetest4 === 'function';
    if (!hasInit && !hasCtor) {{
      st('无法加载极验脚本\\n已尝试内嵌/在线 gt4.js\\n请检查网络或系统代理后重试');
      return;
    }}
    if (hasInit) {{
      st('极验脚本已加载，初始化…');
      initGeetest4({{
        captcha_id: GT,
        captchaId: GT,
        product: 'bind',
        protocol: 'https://',
        riskType: RISK || 'icon',
        userInfo: JSON.stringify({{ session_id: SESSION }})
      }}, function (captcha) {{
        st('极验已初始化，等待就绪…');
        captcha.onReady(function () {{
          var trigger = captcha.showCaptcha || captcha.verify;
          if (typeof trigger === 'function') {{
            st('请完成验证…');
            trigger.call(captcha);
          }} else {{
            st('等待校验结果…');
          }}
        }});
        captcha.onSuccess(function (result) {{ finish(result); }});
        captcha.onError(function (e) {{
          st('验证出错：' + (e && e.msg ? e.msg : JSON.stringify(e)));
        }});
        captcha.onClose(function () {{ st('验证被关闭，可重新发送短信触发。'); }});
        var polls = 0;
        var iv = setInterval(function () {{
          polls++;
          var v = null;
          try {{ v = captcha.getValidate ? captcha.getValidate() : null; }} catch (e) {{}}
          if (v && (v.lot_number || v.captcha_output || v.pass_token)) {{
            clearInterval(iv);
            finish(v);
          }}
          if (polls > 60) clearInterval(iv);
        }}, 500);
      }});
      return;
    }}
    st('使用兼容模式初始化…');
    var config = {{ captchaId: GT, captcha_id: GT, product: 'bind', protocol: 'https://' }};
    if (RISK) config.riskType = RISK;
    var handler = new Geetest4(config);
    if (handler.onReady) {{
      handler.onReady(function () {{
        if (handler.showCaptcha) handler.showCaptcha();
        else if (handler.verify) handler.verify();
      }});
    }}
    if (handler.onSuccess) {{
      handler.onSuccess(function () {{
        finish(handler.getValidate ? handler.getValidate() : {{}});
      }});
    }}
    if (handler.onError) {{
      handler.onError(function (e) {{ st('验证出错：' + (e && e.msg ? e.msg : 'unknown')); }});
    }}
  }}

  if (document.readyState === 'complete') start();
  else window.addEventListener('load', start);
}})();
</script>
</body>
</html>
"""


def _serve_html(gt: str, session_id: str, risk_type: str):
    done = Event()
    result: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_GET(self):
            body = _html_for(gt, session_id, risk_type, port=self.server.server_port).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/done":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            try:
                result["validate"] = json.loads(body.decode("utf-8"))
            except Exception:
                result["validate"] = {}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            done.set()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    return server, url, done, result


_PARENT_RECT: tuple[int, int, int, int] | None = None


def set_parent_rect(rect: tuple[int, int, int, int] | None) -> None:
    """登记父窗口矩形（x, y, w, h），图形验证窗口将居中于其上。"""
    global _PARENT_RECT
    _PARENT_RECT = rect


def _dialog_pos(width: int, height: int, rect=None) -> tuple[int | None, int | None]:
    rect = rect if rect is not None else _PARENT_RECT
    if not rect:
        return None, None
    try:
        px, py, pw, ph = rect
        return int(px + (pw - width) / 2), int(py + (ph - height) / 2)
    except Exception:
        return None, None


def _webview_worker(url: str, out_path: str, rect=None) -> None:
    try:
        import webview
    except Exception:
        Path(out_path).write_text("", encoding="utf-8")
        return
    result: dict[str, Any] = {}

    class Api:
        def on_success(self, data):
            result["validate"] = data
            try:
                if webview.windows:
                    webview.windows[0].destroy()
            except Exception:
                pass
            return True

    try:
        x, y = _dialog_pos(420, 560, rect)
        webview.create_window(
            "米游社 · 图形验证",
            url=url,
            js_api=Api(),
            width=420,
            height=560,
            x=x,
            y=y,
            on_top=True,
            easy_drag=False,
        )
        webview.start(debug=False)
    except Exception:
        pass
    Path(out_path).write_text(
        json.dumps(result.get("validate") or {}, ensure_ascii=False),
        encoding="utf-8",
    )


def _solve_webview_process(url: str, timeout: int = 120) -> dict | None:
    fd, out_path = tempfile.mkstemp(prefix="aigis_", suffix=".json")
    os.close(fd)
    Path(out_path).write_text("{}", encoding="utf-8")
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=_webview_worker, args=(url, out_path, _PARENT_RECT), daemon=True)
    try:
        proc.start()
        proc.join(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    data = {}
    try:
        raw = Path(out_path).read_text(encoding="utf-8").strip()
        if raw:
            data = json.loads(raw)
    except Exception:
        data = {}
    try:
        Path(out_path).unlink(missing_ok=True)
    except Exception:
        pass
    return data or None


def solve_aigis_in_window(
    gt: str, session_id: str, risk_type: str = "icon", timeout: int = 120
) -> str | None:
    if not gt or not session_id:
        return None

    server, url, done, result = _serve_html(gt, session_id, risk_type)
    validate = None
    try:
        validate = _solve_webview_process(url, timeout=timeout)
        if not validate and not done.is_set():
            try:
                webbrowser.open(url)
            except Exception:
                pass
            done.wait(timeout=timeout)
            validate = result.get("validate") or None
        elif not validate:
            validate = result.get("validate") or None
    finally:
        try:
            server.shutdown()
        except Exception:
            pass

    if not validate:
        return None
    captcha_id = validate.get("_captcha_id") or gt
    return build_aigis_header(session_id, captcha_id, validate)


def solve_aigis_challenge(challenge: dict) -> str | None:
    return solve_aigis_in_window(
        gt=challenge.get("gt") or "",
        session_id=challenge.get("session_id") or "",
        risk_type=challenge.get("risk_type") or "icon",
    )
