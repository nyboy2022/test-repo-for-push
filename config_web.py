"""Local-only browser administration console."""
from __future__ import annotations

import atexit
import ctypes
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, jsonify, render_template, request

from redaction import redact, redact_value

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"; STATUS_PATH = BASE_DIR / "runtime_status.json"
FORWARDER_PATH = BASE_DIR / "discord_forwarder.py"; AUDIT_PATH = BASE_DIR / "admin_audit.log"
ADMIN_STATE_PATH = BASE_DIR / "admin_state.json"
HOST = "127.0.0.1"; PORT = 5151
# A page on another site can point its own domain at 127.0.0.1, so the address alone proves nothing.
ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]"}
EXIT_MESSAGES = {2: "所有账号的 Token 都无效或已失效", 3: "已有另一个转发进程在运行，请先关闭它"}
app = Flask(__name__); app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
ADMIN_TOKEN = secrets.token_urlsafe(32)
_proc: subprocess.Popen | None = None; _proc_started_at: float | None = None; _intentional_stop = False
_last_exit_code: int | None = None; _restart_blocked = False
_lock = threading.RLock(); _logs: deque[dict] = deque(maxlen=1500); _seq = 0
_fast_failures: deque[float] = deque(maxlen=3)
_intentional_pids: set[int] = set()
_instance_mutex = None


def audit(action: str, detail: str = "") -> None:
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{action}\t{redact(detail)[:300]}\n")


def append_log(text: str) -> None:
    global _seq
    # Never let credentials escape through the browser log viewer.
    _seq += 1; _logs.append({"seq": _seq, "time": time.strftime("%H:%M:%S"), "text": redact(text).rstrip()})


def load_config_raw() -> dict:
    try: return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {"user_token": "", "forward_rules": [], "debug": False, "options": {}}


def stored_accounts(data: dict) -> list[dict]:
    accounts = data.get("accounts")
    if isinstance(accounts, list) and accounts:
        return [{"id": str(item.get("id", "")).strip(), "name": str(item.get("name", "")).strip(), "user_token": str(item.get("user_token", "")).strip()} for item in accounts if isinstance(item, dict)]
    return [{"id": "default", "name": "主账号", "user_token": str(data.get("user_token", "")).strip()}]


def webhook_id_of(url: str) -> str:
    """The numeric ID is not secret; only the part after it is."""
    parts = urlparse(url).path.split("/")
    return parts[3] if len(parts) > 3 else ""


def public_config() -> dict:
    data = load_config_raw(); options = data.get("options") or {}; accounts = stored_accounts(data); default_id = accounts[0]["id"] if accounts else "default"
    public_accounts = [{"id": item["id"], "name": item["name"] or item["id"], "token_configured": bool(item["user_token"]), "token_hint": ("••••" + item["user_token"][-4:]) if item["user_token"] else ""} for item in accounts]
    rules = []
    for item in data.get("forward_rules", []):
        channel_id = str(item.get("channel_id", "")); url = str(item.get("webhook_url", ""))
        rules.append({"account_id": str(item.get("account_id") or default_id), "channel_id": channel_id, "note": str(item.get("note", "")), "webhook_id": webhook_id_of(url), "webhook_configured": bool(url), "webhook_hint": ("••••" + url[-6:]) if url else "", "allow_automated": bool(item.get("allow_automated", False))})
    primary = accounts[0]["user_token"] if accounts else ""
    return {"accounts": public_accounts, "token_configured": bool(primary), "token_hint": ("••••" + primary[-4:]) if primary else "", "forward_rules": rules, "debug": bool(data.get("debug", False)), "options": {"ignore_bots": bool(options.get("ignore_bots", True)), "max_retries": int(options.get("max_retries", 4)), "queue_size": int(options.get("queue_size", 500)), "auto_restart": bool(options.get("auto_restart", True)), "auto_start": bool(options.get("auto_start", True))}}


def valid_webhook(url: str) -> bool:
    try: value = urlparse(url)
    except ValueError: return False
    return value.scheme == "https" and value.hostname in {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com", "ptb.discordapp.com", "canary.discordapp.com"} and value.path.startswith("/api/webhooks/") and len(value.path.split("/")) >= 5


def normalize_config(payload: dict) -> dict:
    current = load_config_raw(); current_accounts = {item["id"]: item for item in stored_accounts(current)}
    incoming_accounts = payload.get("accounts")
    if incoming_accounts is None:
        accounts = stored_accounts(current)
        if payload.get("user_token"):
            accounts[0]["user_token"] = str(payload["user_token"]).strip()
    else:
        if not isinstance(incoming_accounts, list) or not incoming_accounts: raise ValueError("至少需要一个 Discord 账号")
        accounts = []
        for index, item in enumerate(incoming_accounts):
            if not isinstance(item, dict): raise ValueError("账号配置格式无效")
            account_id = str(item.get("id", "")).strip(); name = str(item.get("name", "")).strip() or f"账号 {index + 1}"
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id): raise ValueError("账号 ID 格式无效")
            token = str(item.get("user_token", "")).strip() or str(current_accounts.get(account_id, {}).get("user_token", "")).strip()
            if len(token) < 20: raise ValueError(f"{name} 的 User Token 未配置或格式无效")
            accounts.append({"id": account_id, "name": name[:40], "user_token": token})
    account_ids = {item["id"] for item in accounts}
    if len(account_ids) != len(accounts): raise ValueError("账号 ID 不能重复")
    default_id = accounts[0]["id"]
    current_default = stored_accounts(current)[0]["id"]
    existing = {(str(item.get("account_id") or current_default), str(item.get("channel_id", "")).strip()): str(item.get("webhook_url", "")).strip() for item in current.get("forward_rules", [])}
    # Moving a rule to another account keeps its webhook when the channel has only one.
    urls_by_channel: dict[str, set[str]] = {}
    for (_, channel), url in existing.items(): urls_by_channel.setdefault(channel, set()).add(url)
    rules = []; seen: set[tuple[str, int]] = set()
    for item in payload.get("forward_rules", []):
        try:
            account_id = str(item.get("account_id") or default_id).strip(); channel_text = str(item.get("channel_id", "")).strip(); channel_id = int(channel_text)
            only_url = next(iter(urls_by_channel[channel_text])) if len(urls_by_channel.get(channel_text, ())) == 1 else ""
            url = str(item.get("webhook_url", "")).strip() or existing.get((account_id, channel_text), "") or only_url
        except (AttributeError, TypeError, ValueError): raise ValueError("频道 ID 必须是数字")
        if account_id not in account_ids: raise ValueError(f"频道 {channel_id} 未选择有效账号")
        key = (account_id, channel_id)
        if channel_id <= 0 or key in seen: raise ValueError("同一账号的频道 ID 无效或重复")
        if not valid_webhook(url): raise ValueError(f"频道 {channel_id} 的 Webhook 地址无效")
        rule = {"account_id": account_id, "channel_id": str(channel_id), "webhook_url": url, "allow_automated": bool(item.get("allow_automated", False))}
        note = str(item.get("note", "")).strip()[:60]
        if note: rule["note"] = note
        seen.add(key); rules.append(rule)
    if not rules: raise ValueError("至少需要一条转发规则")
    options = payload.get("options") or {}
    return {"user_token": accounts[0]["user_token"], "accounts": accounts, "forward_rules": rules, "debug": bool(payload.get("debug", False)), "options": {"ignore_bots": bool(options.get("ignore_bots", True)), "max_retries": max(0, min(int(options.get("max_retries", 4)), 10)), "queue_size": max(10, min(int(options.get("queue_size", 500)), 5000)), "auto_restart": bool(options.get("auto_restart", True)), "auto_start": bool(options.get("auto_start", True))}}


def save_config(data: dict) -> None:
    temporary = CONFIG_PATH.with_suffix(".tmp"); temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"); temporary.replace(CONFIG_PATH)


def is_running() -> bool: return bool(_proc and _proc.poll() is None)


def read_runtime() -> dict:
    try:
        data = redact_value(json.loads(STATUS_PATH.read_text(encoding="utf-8"))); data["stale"] = time.time() - float(data.get("updated_at", 0)) > 30; return data
    except (OSError, ValueError, json.JSONDecodeError): return {}


def config_changed_since_start() -> bool:
    """Saved settings only apply after a restart, so the page has to say when one is pending."""
    if not is_running() or _proc_started_at is None: return False
    try: return CONFIG_PATH.stat().st_mtime > _proc_started_at
    except OSError: return False


def remember_desired_state(running: bool) -> None:
    """A manual stop has to survive the next launch of this console."""
    try: ADMIN_STATE_PATH.write_text(json.dumps({"forwarder_running": running}), encoding="utf-8")
    except OSError as exc: append_log(f"[管理端] 无法保存运行状态: {exc}")


def desired_running() -> bool:
    try: return bool(json.loads(ADMIN_STATE_PATH.read_text(encoding="utf-8")).get("forwarder_running", True))
    except (OSError, ValueError, AttributeError): return True


def reader(proc: subprocess.Popen, started_at: float) -> None:
    global _last_exit_code, _restart_blocked
    if proc.stdout:
        for line in proc.stdout: append_log(line)
    code = proc.wait(); _last_exit_code = code; audit("process_exit", str(code))
    append_log(f"[管理端] 转发进程已退出，代码 {code}" + (f"：{EXIT_MESSAGES[code]}" if code in EXIT_MESSAGES else ""))
    intentional = proc.pid in _intentional_pids
    _intentional_pids.discard(proc.pid)
    if intentional:
        return
    if time.time() - started_at < 15:
        _fast_failures.append(time.time())
    else:
        _fast_failures.clear()
    _restart_blocked = len(_fast_failures) == 3 and _fast_failures[-1] - _fast_failures[0] < 60


def reset_restart_breaker() -> None:
    global _restart_blocked
    _fast_failures.clear(); _restart_blocked = False


def start_process() -> int:
    global _proc, _proc_started_at, _intentional_stop, _last_exit_code, _restart_blocked
    with _lock:
        if is_running(): return int(_proc.pid)
        raw = load_config_raw(); normalize_config({"user_token": "", "forward_rules": raw.get("forward_rules", []), "debug": raw.get("debug", False), "options": raw.get("options", {})})
        _intentional_stop = False; _last_exit_code = None; _restart_blocked = False; env = os.environ.copy(); env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1", TZ="UTC")
        _proc = subprocess.Popen([sys.executable, str(FORWARDER_PATH)], cwd=BASE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
        _proc_started_at = time.time(); threading.Thread(target=reader, args=(_proc, _proc_started_at), daemon=True).start(); append_log(f"[管理端] 已启动 PID {_proc.pid}"); audit("start", f"pid={_proc.pid}"); remember_desired_state(True); return int(_proc.pid)


def stop_process(remember: bool = True) -> None:
    global _intentional_stop
    with _lock:
        _intentional_stop = True
        if remember: remember_desired_state(False)
        if not is_running(): return
        assert _proc is not None; _intentional_pids.add(_proc.pid); _proc.terminate()
        try: _proc.wait(timeout=8)
        except subprocess.TimeoutExpired: _proc.kill(); _proc.wait(timeout=3)
        audit("stop")


def supervisor() -> None:
    while True:
        time.sleep(3); options = load_config_raw().get("options") or {}
        if _restart_blocked:
            continue
        if _proc and not is_running() and not _intentional_stop and bool(options.get("auto_restart", True)):
            append_log("[管理端] 检测到异常退出，正在自动重启")
            try: start_process()
            except Exception as exc: append_log(f"[管理端] 自动重启失败: {type(exc).__name__}: {exc}")


def autostart() -> None:
    options = load_config_raw().get("options") or {}
    if not bool(options.get("auto_start", True)) or not desired_running(): return
    try: start_process(); append_log("[管理端] 已自动开始转发")
    except Exception as exc: append_log(f"[管理端] 自动启动失败: {type(exc).__name__}: {exc}")


def probe_webhook(url: str) -> dict:
    """Read the webhook's public details. GET never posts anything to the channel."""
    import requests
    try:
        response = requests.get(url, timeout=(5, 10))
    except requests.RequestException as exc:
        return {"ok": False, "status": None, "error": redact(type(exc).__name__)}
    if response.status_code != 200:
        return {"ok": False, "status": response.status_code, "error": f"HTTP {response.status_code}"}
    data = response.json()
    return {"ok": True, "status": 200, "name": str(data.get("name") or ""), "channel_id": str(data.get("channel_id") or ""), "guild_id": str(data.get("guild_id") or "")}


def check_webhooks() -> dict:
    rules = load_config_raw().get("forward_rules", [])
    urls = {webhook_id_of(str(item.get("webhook_url", ""))): str(item.get("webhook_url", "")) for item in rules if valid_webhook(str(item.get("webhook_url", "")))}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(zip(urls, pool.map(probe_webhook, urls.values())))
    sources = {str(item.get("channel_id", "")) for item in rules}
    for result in results.values():
        result["loop_risk"] = bool(result.get("channel_id")) and result.get("channel_id") in sources
    return results


@app.before_request
def guard():
    if request.remote_addr not in {"127.0.0.1", "::1"}: return jsonify({"ok": False, "error": "仅允许本机访问"}), 403
    if request.host.rsplit(":", 1)[0] not in ALLOWED_HOSTNAMES and request.host not in ALLOWED_HOSTNAMES: return jsonify({"ok": False, "error": "请求来源无效"}), 403
    if request.method in {"POST", "PUT", "DELETE", "PATCH"} and request.headers.get("X-Admin-Token") != ADMIN_TOKEN: return jsonify({"ok": False, "error": "请求验证失败，请刷新页面"}), 403


@app.after_request
def security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    return response


@app.get("/")
def index(): return render_template("index.html", admin_token=ADMIN_TOKEN)

@app.get("/api/config")
def api_config(): return jsonify({"ok": True, "config": public_config()})

@app.put("/api/config")
def api_save():
    try:
        data = normalize_config(request.get_json(silent=False) or {}); save_config(data); audit("config_save", f"rules={len(data['forward_rules'])}"); return jsonify({"ok": True, "config": public_config()})
    except (TypeError, ValueError) as exc: return jsonify({"ok": False, "error": str(exc)}), 400

@app.get("/api/status")
def api_status():
    return jsonify({"ok": True, "running": is_running(), "pid": _proc.pid if is_running() else None, "started_at": _proc_started_at, "last_exit_code": _last_exit_code, "exit_message": EXIT_MESSAGES.get(_last_exit_code), "restart_blocked": _restart_blocked, "restart_required": config_changed_since_start(), "runtime": read_runtime()})

@app.post("/api/process/<action>")
def api_process(action: str):
    try:
        if action == "start": reset_restart_breaker(); return jsonify({"ok": True, "pid": start_process()})
        if action == "stop": stop_process(); return jsonify({"ok": True})
        if action == "restart": stop_process(); reset_restart_breaker(); pid = start_process(); audit("restart", f"pid={pid}"); return jsonify({"ok": True, "pid": pid})
        return jsonify({"ok": False, "error": "未知操作"}), 404
    except Exception as exc: return jsonify({"ok": False, "error": redact(f"{type(exc).__name__}: {exc}")}), 400

@app.post("/api/webhooks/check")
def api_check_webhooks():
    return jsonify({"ok": True, "results": check_webhooks()})

@app.get("/api/logs")
def api_logs():
    try: since = max(0, int(request.args.get("since", 0)))
    except ValueError: since = 0
    return jsonify({"ok": True, "lines": [line for line in _logs if line["seq"] > since]})


@app.get("/api/diagnostics")
def api_diagnostics():
    config = load_config_raw()
    try: log_size = (BASE_DIR / "forwarder.log").stat().st_size
    except OSError: log_size = 0
    return jsonify({"ok": True, "diagnostics": {"python": sys.version.split()[0], "executable": sys.executable, "base_dir": str(BASE_DIR), "config_readable": CONFIG_PATH.is_file(), "token_configured": all(bool(item["user_token"]) for item in stored_accounts(config)), "account_count": len(stored_accounts(config)), "rule_count": len(config.get("forward_rules") or []), "forwarder_exists": FORWARDER_PATH.is_file(), "platform": sys.platform, "log_bytes": log_size}})


def main() -> None:
    global _instance_mutex
    if os.name == "nt":
        _instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DiscordForwarderAdmin-5151")
        if ctypes.windll.kernel32.GetLastError() == 183:
            webbrowser.open(f"http://{HOST}:{PORT}")
            print("管理后台已经运行，已打开现有页面。")
            return
    threading.Thread(target=supervisor, daemon=True).start()
    autostart()
    if os.environ.get("DISCORD_FORWARDER_NO_BROWSER") != "1":
        threading.Thread(target=lambda: (time.sleep(.8), webbrowser.open(f"http://{HOST}:{PORT}")), daemon=True).start()
    print(f"管理后台：http://{HOST}:{PORT}"); app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)


atexit.register(stop_process, remember=False)
if __name__ == "__main__": main()
