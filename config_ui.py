"""
Discord 消息转发 - 配置界面
支持：频道 -> Webhook 一对一映射
"""

import json
import subprocess
import sys
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont


CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    """加载配置"""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "user_token": "",
        "forward_rules": [],
        "debug": True,
    }


def save_config(data: dict) -> bool:
    """保存配置"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except IOError:
        return False


class ConfigApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Discord 消息转发 - 配置")
        self.root.geometry("720x520")
        self.root.resizable(True, True)
        self.root.minsize(560, 450)

        main = ttk.Frame(self.root, padding=15)
        main.pack(fill=tk.BOTH, expand=True)

        # === User Token ===
        ttk.Label(main, text="User Token:").pack(anchor=tk.W, pady=(0, 2))
        self.token_var = tk.StringVar()
        self.token_entry = ttk.Entry(main, textvariable=self.token_var, show="•", width=70)
        self.token_entry.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(main, text="（self-bot 高风险：可能违反 Discord 规则并导致封号，切勿分享 Token）", font=("", 8), foreground="gray").pack(anchor=tk.W, pady=(0, 8))

        # === 转发规则 ===
        ttk.Label(main, text="转发规则（每个频道对应一个 Webhook）:").pack(anchor=tk.W, pady=(0, 2))
        rules_frame = ttk.Frame(main)
        rules_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        # 表头
        head = ttk.Frame(rules_frame)
        head.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(head, text="频道 ID", width=22).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(head, text="Webhook URL", width=45).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(head, text="", width=6).pack(side=tk.LEFT)

        # 可滚动区域
        canvas = tk.Canvas(rules_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(rules_frame, orient=tk.VERTICAL, command=canvas.yview)
        self.rules_container = ttk.Frame(canvas)

        self.rules_container.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.rules_container, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind("<MouseWheel>", _on_mousewheel)

        self.rule_rows: list[tuple[tk.StringVar, tk.StringVar, ttk.Frame]] = []

        ttk.Label(main, text="（右键频道→复制 ID | 频道设置→集成→Webhook→复制链接）", font=("", 8), foreground="gray").pack(anchor=tk.W, pady=(0, 5))

        # 添加规则按钮
        ttk.Button(main, text="+ 添加规则", command=self.add_rule_row).pack(anchor=tk.W, pady=(0, 8))

        # === 调试模式 ===
        self.debug_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            main,
            text="开启调试模式（控制台打印收到的消息和频道 ID）",
            variable=self.debug_var,
        ).pack(anchor=tk.W, pady=(5, 10))

        # === 按钮区 ===
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btn_frame, text="保存配置", command=self.on_save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="启动转发", command=self.on_start).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="退出", command=self.root.quit).pack(side=tk.LEFT)

        self.token_entry.bind("<Button-3>", self.toggle_token_visibility)
        self.load_values()

    def toggle_token_visibility(self, event):
        if self.token_entry["show"] == "•":
            self.token_entry["show"] = ""
        else:
            self.token_entry["show"] = "•"

    def add_rule_row(self, channel_id: str = "", webhook_url: str = ""):
        """添加一行规则"""
        row = ttk.Frame(self.rules_container)
        row.pack(fill=tk.X, pady=2)
        cid_var = tk.StringVar(value=channel_id)
        wh_var = tk.StringVar(value=webhook_url)
        ttk.Entry(row, textvariable=cid_var, width=24).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Entry(row, textvariable=wh_var, width=50).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(row, text="删除", width=6, command=lambda: self.remove_rule_row(row, cid_var, wh_var)).pack(side=tk.LEFT)
        self.rule_rows.append((cid_var, wh_var, row))

    def remove_rule_row(self, row: ttk.Frame, cid_var, wh_var):
        row.destroy()
        self.rule_rows[:] = [(c, w, r) for c, w, r in self.rule_rows if r != row]

    def load_values(self):
        cfg = load_config()
        self.token_var.set(cfg.get("user_token", ""))
        self.debug_var.set(cfg.get("debug", True))

        # 清空现有行
        for _, _, r in self.rule_rows[:]:
            r.destroy()
        self.rule_rows.clear()

        # 加载规则
        rules = cfg.get("forward_rules", [])
        if rules:
            for r in rules:
                if isinstance(r, dict):
                    cid = str(r.get("channel_id", ""))
                    wh = str(r.get("webhook_url", ""))
                    self.add_rule_row(cid, wh)
        else:
            # 兼容旧格式
            ids = cfg.get("source_channel_ids", [])
            wh = cfg.get("webhook_url", "")
            if ids and wh:
                for x in ids:
                    self.add_rule_row(str(x), wh)
            else:
                self.add_rule_row()  # 至少一行空

    def get_forward_rules(self) -> list[dict]:
        rules = []
        for cid_var, wh_var, _ in self.rule_rows:
            cid = cid_var.get().strip()
            wh = wh_var.get().strip()
            if cid and cid.isdigit() and wh and "discord.com/api/webhooks" in wh:
                rules.append({"channel_id": cid, "webhook_url": wh})
        return rules

    def on_save(self):
        data = {
            "user_token": self.token_var.get().strip(),
            "forward_rules": self.get_forward_rules(),
            "debug": self.debug_var.get(),
        }
        if save_config(data):
            messagebox.showinfo("提示", "配置已保存")
        else:
            messagebox.showerror("错误", "保存失败")

    def validate_and_save(self) -> bool:
        token = self.token_var.get().strip()
        rules = self.get_forward_rules()

        if not token:
            messagebox.showwarning("提示", "请填写 User Token")
            return False
        if not rules:
            messagebox.showwarning("提示", "请至少添加一条转发规则（频道 ID + Webhook URL）")
            return False

        data = {
            "user_token": token,
            "forward_rules": rules,
            "debug": self.debug_var.get(),
        }
        if not save_config(data):
            messagebox.showerror("错误", "保存配置失败")
            return False
        return True

    def on_start(self):
        if not self.validate_and_save():
            return
        self.root.withdraw()
        try:
            subprocess.run(
                [sys.executable, str(Path(__file__).parent / "discord_forwarder.py")],
                cwd=str(Path(__file__).parent),
            )
        finally:
            self.root.deiconify()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = ConfigApp()
    app.run()
