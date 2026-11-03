"""
Discord 消息转发 - Web 配置界面
访问 http://127.0.0.1:5151 在浏览器中编辑配置、启动/停止转发、查看实时日志
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template_string, request


BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
FORWARDER_PATH = BASE_DIR / "discord_forwarder.py"
HOST = "127.0.0.1"
PORT = 5151

app = Flask(__name__)

_proc_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_log_buf: deque[str] = deque(maxlen=1000)
_log_seq = 0
_log_lock = threading.Lock()


def append_log(line: str) -> None:
    global _log_seq
    with _log_lock:
        _log_seq += 1
        _log_buf.append(f"{_log_seq}\t{line.rstrip()}")


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"user_token": "", "forward_rules": [], "debug": True}


def save_config(data: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_valid_webhook_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = (parsed.netloc or "").lower()
    allowed_hosts = {
        "discord.com",
        "discordapp.com",
        "ptb.discord.com",
        "canary.discord.com",
        "ptb.discordapp.com",
        "canary.discordapp.com",
    }
    if host not in allowed_hosts:
        return False

    return parsed.path.startswith("/api/webhooks/")


def normalize_payload(payload: dict) -> dict:
    token = str(payload.get("user_token", "")).strip()
    debug = bool(payload.get("debug", True))
    rules_in = payload.get("forward_rules", []) or []
    rules_out = []
    errors = []
    seen_channel_ids: set[str] = set()

    for index, r in enumerate(rules_in, start=1):
        if not isinstance(r, dict):
            errors.append(f"第 {index} 行不是有效对象")
            continue
        cid = str(r.get("channel_id", "")).strip()
        wh = str(r.get("webhook_url", "")).strip()

        if not cid and not wh:
            continue
        if not cid:
            errors.append(f"第 {index} 行缺少频道 ID")
            continue
        if not cid.isdigit():
            errors.append(f"第 {index} 行频道 ID 不是纯数字")
            continue
        if cid in seen_channel_ids:
            errors.append(f"第 {index} 行频道 ID 重复: {cid}")
            continue
        if not wh:
            errors.append(f"第 {index} 行缺少 Webhook URL")
            continue
        if not is_valid_webhook_url(wh):
            errors.append(f"第 {index} 行 Webhook URL 格式无效")
            continue

        seen_channel_ids.add(cid)
        rules_out.append({"channel_id": cid, "webhook_url": wh})

    if errors:
        raise ValueError("；".join(errors))

    return {"user_token": token, "forward_rules": rules_out, "debug": debug}


def reader_thread(proc: subprocess.Popen) -> None:
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, ""):
        if not raw:
            break
        append_log(raw)
    proc.stdout.close()
    rc = proc.wait()
    append_log(f"[进程已退出] exit code = {rc}")


# ==================== HTML ====================
INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Discord 消息转发 - 配置</title>
<style>
:root {
  --bg: #0f1115;
  --panel: #171a21;
  --panel2: #1f242d;
  --border: #2a313d;
  --text: #e6e8ec;
  --muted: #8b94a3;
  --accent: #5865f2;
  --accent-hov: #6b78ff;
  --danger: #ed4245;
  --ok: #3ba55d;
  --warn: #faa61a;
}
* { box-sizing: border-box; }
body {
  margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--text); font-size: 14px;
}
.wrap { max-width: 980px; margin: 0 auto; padding: 24px 20px 40px; }
h1 { font-size: 18px; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: 12px; margin-bottom: 18px; }
.card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px 18px; margin-bottom: 16px;
}
.card h2 { font-size: 14px; margin: 0 0 10px; font-weight: 600; }
label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
input[type="text"], input[type="password"] {
  width: 100%; padding: 8px 10px; border-radius: 6px; border: 1px solid var(--border);
  background: var(--panel2); color: var(--text); font-size: 13px; font-family: inherit;
}
input:focus { outline: none; border-color: var(--accent); }
.field-err { border-color: var(--danger) !important; box-shadow: 0 0 0 1px rgba(237, 66, 69, 0.28); }
.hint { color: var(--muted); font-size: 11px; margin-top: 4px; }
.row { display: grid; grid-template-columns: 220px 1fr 70px; gap: 8px; margin-bottom: 6px; align-items: center; }
.row .head { color: var(--muted); font-size: 11px; padding: 0 2px; }
button {
  border: 0; border-radius: 6px; padding: 7px 14px; font-size: 13px; cursor: pointer;
  background: var(--panel2); color: var(--text); border: 1px solid var(--border);
  font-family: inherit;
}
button:hover { background: #262d39; }
button.primary { background: var(--accent); border-color: var(--accent); }
button.primary:hover { background: var(--accent-hov); border-color: var(--accent-hov); }
button.danger { background: var(--danger); border-color: var(--danger); }
button.danger:hover { filter: brightness(1.1); }
button.ghost { background: transparent; }
button:disabled { opacity: 0.45; cursor: not-allowed; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.checkbox { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text); cursor: pointer; }
.token-wrap { position: relative; }
.token-wrap button { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); padding: 3px 8px; font-size: 11px; }
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #555; }
.dot.run { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
.dot.stop { background: var(--muted); }
.toast {
  position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
  background: var(--panel2); border: 1px solid var(--border); border-radius: 6px;
  padding: 8px 14px; font-size: 12px; opacity: 0; transition: opacity .2s;
  pointer-events: none; z-index: 100;
}
.toast.show { opacity: 1; }
.toast.ok { border-color: var(--ok); }
.toast.err { border-color: var(--danger); }
#log {
  background: #0a0c10; border: 1px solid var(--border); border-radius: 6px;
  height: 320px; overflow-y: auto; padding: 10px 12px; font-family: ui-monospace, "SF Mono", Consolas, monospace;
  font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
}
#log .empty { color: var(--muted); font-style: italic; }
.flex-between { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Discord 消息转发 — 配置</h1>
  <div class="sub">在浏览器里编辑配置、启动 / 停止转发、查看实时日志</div>

  <div class="card">
    <h2>User Token</h2>
    <div class="token-wrap">
      <input id="token" type="password" placeholder="普通用户 Token（self-bot，高风险）" autocomplete="off">
      <button type="button" id="toggle-token">显示</button>
    </div>
    <div class="hint">⚠️ self-bot 可能违反 Discord 规则并导致封号；Token 等同于密码，切勿外泄</div>
  </div>

  <div class="card">
    <div class="flex-between" style="margin-bottom: 10px;">
      <h2 style="margin:0;">转发规则(每个频道对应一个 Webhook)</h2>
      <button type="button" id="add-rule">+ 添加规则</button>
    </div>
    <div class="row head">
      <div>频道 ID</div>
      <div>Webhook URL</div>
      <div></div>
    </div>
    <div id="rules"></div>
    <div class="hint">右键频道 → 复制 ID  |  频道设置 → 集成 → Webhook → 复制链接</div>
  </div>

  <div class="card">
    <label class="checkbox">
      <input type="checkbox" id="debug">
      开启调试模式(控制台打印收到的消息和频道 ID)
    </label>
  </div>

  <div class="card">
    <div class="flex-between">
      <div class="actions">
        <button class="primary" id="save">保存配置</button>
        <button class="primary" id="start">启动转发</button>
        <button class="danger" id="stop" disabled>停止</button>
        <button class="ghost" id="clear-log">清空日志</button>
      </div>
      <div class="status"><span class="dot stop" id="status-dot"></span><span id="status-text">未运行</span></div>
    </div>
  </div>

  <div class="card">
    <h2>运行日志</h2>
    <div id="log"><span class="empty">(尚未启动)</span></div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
const $ = (s) => document.querySelector(s);
const rulesEl = $("#rules");

function addRule(channel_id = "", webhook_url = "") {
  const row = document.createElement("div");
  row.className = "row";
  row.innerHTML = `
    <input type="text" class="cid" placeholder="例如 922638571679141898">
    <input type="text" class="wh" placeholder="https://discord.com/api/webhooks/...">
    <button type="button" class="del">删除</button>
  `;
  row.querySelector(".cid").value = channel_id;
  row.querySelector(".wh").value = webhook_url;
  row.querySelector(".del").addEventListener("click", () => row.remove());
  rulesEl.appendChild(row);
}

function gatherConfig() {
  const rules = [...rulesEl.querySelectorAll(".row")].map(r => ({
    channel_id: r.querySelector(".cid").value.trim(),
    webhook_url: r.querySelector(".wh").value.trim(),
  })).filter(r => r.channel_id || r.webhook_url);
  return {
    user_token: $("#token").value.trim(),
    forward_rules: rules,
    debug: $("#debug").checked,
  };
}

function clearRuleErrors() {
  for (const input of rulesEl.querySelectorAll(".cid, .wh")) input.classList.remove("field-err");
}

function validateRules() {
  clearRuleErrors();
  const errors = [];
  const seen = new Set();

  [...rulesEl.querySelectorAll(".row")].forEach((row, idx) => {
    const cidInput = row.querySelector(".cid");
    const whInput = row.querySelector(".wh");
    const cid = cidInput.value.trim();
    const wh = whInput.value.trim();
    const rowNo = idx + 1;

    if (!cid && !wh) return;
    if (!cid) {
      cidInput.classList.add("field-err");
      errors.push(`第 ${rowNo} 行缺少频道 ID`);
      return;
    }
    if (!/^\d+$/.test(cid)) {
      cidInput.classList.add("field-err");
      errors.push(`第 ${rowNo} 行频道 ID 不是纯数字`);
      return;
    }
    if (seen.has(cid)) {
      cidInput.classList.add("field-err");
      errors.push(`第 ${rowNo} 行频道 ID 重复`);
      return;
    }
    if (!wh) {
      whInput.classList.add("field-err");
      errors.push(`第 ${rowNo} 行缺少 Webhook URL`);
      return;
    }
    if (!/^https?:\/\/(?:canary\.|ptb\.)?discord(?:app)?\.com\/api\/webhooks\/.+/i.test(wh)) {
      whInput.classList.add("field-err");
      errors.push(`第 ${rowNo} 行 Webhook URL 格式无效`);
      return;
    }

    seen.add(cid);
  });

  return errors;
}

let toastTimer = null;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 1800);
}

async function loadConfig() {
  const r = await fetch("/api/config");
  const cfg = await r.json();
  $("#token").value = cfg.user_token || "";
  $("#debug").checked = cfg.debug !== false;
  rulesEl.innerHTML = "";
  (cfg.forward_rules || []).forEach(rule => addRule(rule.channel_id, rule.webhook_url));
  if (rulesEl.children.length === 0) addRule();
}

async function saveConfig() {
  const errors = validateRules();
  if (errors.length) {
    toast(errors[0], "err");
    return false;
  }

  const r = await fetch("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(gatherConfig()),
  });
  const j = await r.json();
  if (j.ok) toast("已保存 ✓", "ok"); else toast("保存失败: " + (j.error || ""), "err");
  return j.ok;
}

async function startForwarder() {
  if (!await saveConfig()) return;
  const r = await fetch("/api/start", { method: "POST" });
  const j = await r.json();
  if (j.ok) toast("已启动 ✓", "ok"); else toast(j.error || "启动失败", "err");
}

async function stopForwarder() {
  const r = await fetch("/api/stop", { method: "POST" });
  const j = await r.json();
  if (j.ok) toast("已停止", "ok"); else toast(j.error || "停止失败", "err");
}

let lastSeq = 0;
let logEmpty = true;
async function pollLogs() {
  try {
    const r = await fetch(`/api/logs?since=${lastSeq}`);
    const j = await r.json();
    if (j.lines && j.lines.length) {
      const log = $("#log");
      if (logEmpty) { log.innerHTML = ""; logEmpty = false; }
      const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
      for (const item of j.lines) {
        lastSeq = item.seq;
        const div = document.createElement("div");
        div.textContent = item.text;
        log.appendChild(div);
      }
      while (log.children.length > 800) log.removeChild(log.firstChild);
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
    const dot = $("#status-dot"), txt = $("#status-text");
    if (j.running) {
      dot.className = "dot run"; txt.textContent = "运行中";
      $("#start").disabled = true; $("#stop").disabled = false;
    } else {
      dot.className = "dot stop"; txt.textContent = "未运行";
      $("#start").disabled = false; $("#stop").disabled = true;
    }
  } catch (e) {}
}

$("#add-rule").addEventListener("click", () => addRule());
$("#save").addEventListener("click", saveConfig);
$("#start").addEventListener("click", startForwarder);
$("#stop").addEventListener("click", stopForwarder);
$("#clear-log").addEventListener("click", () => {
  $("#log").innerHTML = '<span class="empty">(已清空)</span>'; logEmpty = true;
});
$("#toggle-token").addEventListener("click", () => {
  const i = $("#token");
  if (i.type === "password") { i.type = "text"; $("#toggle-token").textContent = "隐藏"; }
  else { i.type = "password"; $("#toggle-token").textContent = "显示"; }
});

loadConfig();
setInterval(pollLogs, 1000);
pollLogs();
</script>
</body>
</html>
"""


# ==================== 路由 ====================
@app.get("/")
def index():
    return render_template_string(INDEX_HTML)


@app.get("/api/config")
def api_get_config():
    return jsonify(load_config())


@app.post("/api/config")
def api_save_config():
    try:
        data = normalize_payload(request.get_json(force=True) or {})
        save_config(data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 400


@app.post("/api/start")
def api_start():
    global _proc
    with _proc_lock:
        if _proc and _proc.poll() is None:
            return jsonify({"ok": False, "error": "已经在运行中"}), 400

        cfg = load_config()
        if not cfg.get("user_token") or not cfg.get("forward_rules"):
            return jsonify({"ok": False, "error": "请先填写 Token 并添加至少一条转发规则"}), 400

        _log_buf.clear()
        append_log("[Web] 正在启动 discord_forwarder.py ...")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            _proc = subprocess.Popen(
                [sys.executable, str(FORWARDER_PATH)],
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except Exception as e:
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500

        threading.Thread(target=reader_thread, args=(_proc,), daemon=True).start()
        return jsonify({"ok": True, "pid": _proc.pid})


@app.post("/api/stop")
def api_stop():
    global _proc
    with _proc_lock:
        if not _proc or _proc.poll() is not None:
            return jsonify({"ok": False, "error": "进程未在运行"}), 400
        try:
            if os.name == "nt":
                _proc.terminate()
            else:
                _proc.send_signal(signal.SIGTERM)
            try:
                _proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _proc.kill()
            append_log("[Web] 已停止")
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.get("/api/logs")
def api_logs():
    since = int(request.args.get("since", 0))
    with _log_lock:
        lines = []
        for entry in _log_buf:
            seq_str, _, text = entry.partition("\t")
            seq = int(seq_str)
            if seq > since:
                lines.append({"seq": seq, "text": text})
    running = bool(_proc and _proc.poll() is None)
    return jsonify({"lines": lines, "running": running})


def open_browser_later(url: str) -> None:
    time.sleep(0.8)
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    print(f"配置界面: {url}")
    print("(关闭此窗口或按 Ctrl+C 退出)")
    threading.Thread(target=open_browser_later, args=(url,), daemon=True).start()
    # 关掉 reloader,避免 subprocess / 线程被重复创建
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
