# -*- coding: utf-8 -*-
"""
Epson L3250 清墨器 + 清洗诊断（中文图形界面）
底层调用 EWR v1.6.0 的 --json 契约：
  1. 读出打印机 EEPROM 里的废墨计数器
  2. 把废墨计数器写回初始值（清零），写前自动备份、写后回读校验
  3. 需要时把备份还原回去
  4. 喷嘴清洗 / 强力充墨，并把打印机返回的真实结果（phase / route / 错误码）显示出来
  5. 「清洗诊断」：清洗前后各读一次计数，用差值判断墨到底有没有被抽进废墨垫
安全边界：本工具不刷写固件版本，只写废墨计数器那几十个字节。
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
EWR_DIR = HERE / "EWR"
EWR = EWR_DIR / "ewr.exe"
BACKUP_DIR = EWR_DIR / "backups"
LOG_FILE = HERE / "运行日志.txt"
DIAG = HERE / "l3250_diag.py"
REPORT_FILE = HERE / "诊断报告.txt"

CREATE_NO_WINDOW = 0x08000000

# stage 是契约里的稳定枚举，用它翻中文比解析英文 message 可靠
STAGE_CN = {
    "general": "准备",
    "database": "读型号库",
    "update": "检查更新",
    "detect": "识别打印机",
    "handshake": "建立连接",
    "read": "读取",
    "write": "写入",
    "verify": "回读校验",
    "commit": "提交",
}

ERR_CN = {
    "bad_usage": "命令行参数不对",
    "another_run": "另一个清墨进程正占用打印机，先关掉它",
    "device_not_found": "没找到打印机：检查 USB 线、电源是否打开",
    "interface_not_found": "指定的 USB 接口不存在",
    "database": "型号库 database.json 缺失或损坏",
    "model_required": "识别不出型号，请在右上角手动选 L3250",
    "model_unknown": "型号库里没有这个型号",
    "model_ambiguous": "型号匹配到多个，无法确定",
    "model_mismatch": "你指定的型号和实际接的打印机不一致",
    "not_supported": "这台机器不支持该操作（或该连接方式下锁了 EEPROM）",
    "read_failed": "打印机没回应读取请求",
    "incomplete_dump": "备份没读全，这张备份不能用",
    "blocked": "被安全闸拦住了，未写入任何数据",
    "write_failed": "写入了但中途失败，可能部分已生效",
    "write_unverified": "打印机接受了写入，但回读对不上——没真正写进去",
    "io_error": "文件写不进去（目录权限？）",
    "bad_backup": "还原文件不是这张打印机的备份",
    "send_failed": "维护指令发不出去或被打印机拒绝",
    "not_started": "打印机收到了指令却没动起来",
    "printer_error": "打印机遇到了错误",
    "timed_out": "等太久，打印机会自己做完",
    "failed": "失败了，原因见日志",
}

BLOCKER_PAD_CODES = {16: "SERVICE REQUEST（废墨垫到期）", 44: "CARTRIDGE OVERFLOW（废墨溢出）"}


# ----------------------------------------------------------------------------- 子进程
class Ewr:
    """在后台线程里跑 ewr.exe，按行解析 JSON Lines。"""

    def __init__(self, on_event, on_done, on_fail):
        self.on_event = on_event
        self.on_done = on_done
        self.on_fail = on_fail
        self.proc = None
        self.running = False

    def start(self, args, tag):
        if self.running:
            self.on_fail("上一件事还没做完，等它结束。")
            return False
        if not EWR.exists():
            self.on_fail("找不到 EWR\\ewr.exe，工具目录不完整。")
            return False
        self.running = True
        self.tag = tag
        threading.Thread(target=self._run, args=(args, tag), daemon=True).start()
        return True

    def _run(self, args, tag):
        cmd = [str(EWR)] + args + ["--json", "--no-update"]
        result = None
        stderr = ""
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(EWR_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                t = obj.get("type")
                if t == "event":
                    self.on_event(obj)
                elif t == "result":
                    result = obj
            stderr = (self.proc.stderr.read() or "").strip()
            self.proc.wait(timeout=15)
        except FileNotFoundError:
            stderr = "找不到 ewr.exe"
        except Exception as exc:  # noqa: BLE001
            stderr = f"{type(exc).__name__}: {exc}"
        finally:
            self.running = False
        self.on_done(tag, result, stderr)


class Runner:
    """跑任意子进程（这里是 l3250_diag.py），把每行输出丢回队列，界面能实时看到进度。"""

    def __init__(self, on_line, on_done):
        self.on_line = on_line
        self.on_done = on_done
        self.proc = None
        self.running = False

    def start(self, cmd, tag):
        if self.running:
            return False
        self.running = True
        threading.Thread(target=self._run, args=(cmd, tag), daemon=True).start()
        return True

    def _run(self, cmd, tag):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        returncode = -1
        err = ""
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(HERE),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            for line in self.proc.stdout:
                line = line.rstrip()
                if line:
                    self.on_line(tag, line)
            returncode = self.proc.wait()
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        finally:
            self.running = False
        self.on_done(tag, returncode, err)


def run_sync(args, timeout=120):
    """给自检用：同步跑一条命令，返回 (result, stderr)。"""
    cmd = [str(EWR)] + args + ["--json", "--no-update"]
    result = None
    try:
        p = subprocess.run(
            cmd,
            cwd=str(EWR_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") == "result":
                result = obj
        return result, (p.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


# ----------------------------------------------------------------------------- 自检
def selftest():
    print("== Epson L3250 清墨器 自检 ==")
    print("工具目录 :", HERE)
    print("ewr.exe  :", "存在" if EWR.exists() else "缺失", f"({EWR.stat().st_size if EWR.exists() else 0} 字节)")
    db = EWR_DIR / "database.json"
    print("型号库   :", "存在" if db.exists() else "缺失", f"({db.stat().st_size if db.exists() else 0} 字节)")
    if db.exists():
        try:
            data = json.loads(db.read_text(encoding="utf-8"))
            models = data.get("models") or {}
            ent = models.get("L3250")
            print("型号条目 :", "含 L3250" if ent else "不含 L3250")
            if ent:
                for g in ent.get("pad_groups", []):
                    print(f"   组 {g.get('kind')}: 地址 {g.get('addresses')} → 写入 {g.get('reset')}")
        except Exception as exc:  # noqa: BLE001
            print("型号库读取失败:", exc)

    res, err = run_sync(["--list"], timeout=60)
    print("--list   :", "ok" if (res and res.get("ok")) else (res or {}).get("error_code", "无结果"))
    if res:
        d = res.get("data") or {}
        print("           USB 接口", len(d.get("interfaces") or []), "个；网络打印机", len(d.get("network_printers") or []), "台")
    if err:
        print("           stderr:", err.splitlines()[0][:120] if err.splitlines() else "")

    res, err = run_sync(["--status"], timeout=120)
    print("--status :", "ok" if (res and res.get("ok")) else (res or {}).get("error_code", "无结果"))
    if res and res.get("ok"):
        d = res.get("data") or {}
        pr = d.get("printer") or {}
        print("           型号", d.get("model"), "状态", pr.get("state"), "电池(废墨)垫:")
        for p in d.get("pads") or []:
            print(f"             - {p.get('name')}: {p.get('used')}/{p.get('max')} ({p.get('percent')}%)")
    print("\n自检结束。接上打印机后运行 GUI 才有实际数据。")
    return 0


# ----------------------------------------------------------------------------- GUI
def main_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, font as tkfont

    try:  # 高 DPI 下字不发虚
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass

    root = tk.Tk()
    root.title("Epson L3250 清墨器 + 清洗诊断")
    root.geometry("820x680")
    root.minsize(760, 620)

    base_font = ("Microsoft YaHei UI", 10)
    bold_font = ("Microsoft YaHei UI", 10, "bold")
    big_font = ("Microsoft YaHei UI", 13, "bold")

    style = ttk.Style()
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass
    style.configure(".", font=base_font)
    style.configure("Treeview", font=base_font, rowheight=26)
    style.configure("Treeview.Heading", font=bold_font)
    style.configure("Go.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    app = {"busy": False, "last_status": None}
    q = queue.Queue()

    # ---------------- 日志
    def log(text, tag=""):
        stamp = time.strftime("%H:%M:%S")
        txt.configure(state="normal")
        txt.insert("end", f"[{stamp}] {text}\n", tag)
        # 别让日志无限长
        if int(txt.index("end-1c").split(".")[0]) > 3000:
            txt.delete("1.0", "1.500")
        txt.see("end")
        txt.configure(state="disabled")
        try:
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 进度
    def set_busy(flag, note=""):
        app["busy"] = flag
        state = "disabled" if flag else "normal"
        for b in buttons:
            b.configure(state=state)
        if flag:
            prog.configure(mode="indeterminate")
            prog.start(14)
            banner.configure(text=f"● {note}", foreground="#a05000")
        else:
            prog.stop()
            prog.configure(mode="determinate", value=0)

    def set_banner(text, color):
        banner.configure(text=text, foreground=color)

    # ---------------- ewr 回调
    def on_event(ev):
        q.put(("event", ev))

    def on_done(tag, result, stderr):
        q.put(("done", (tag, result, stderr)))

    def on_fail(msg):
        q.put(("fail", msg))

    ewr = Ewr(on_event, on_done, on_fail)

    def on_line(tag, line):
        q.put(("line", (tag, line)))

    def on_rundone(tag, code, err):
        q.put(("rundone", (tag, code, err)))

    runner = Runner(on_line, on_rundone)

    def drain():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "event":
                    handle_event(payload)
                elif kind == "done":
                    handle_done(*payload)
                elif kind == "line":
                    handle_line(*payload)
                elif kind == "rundone":
                    handle_rundone(*payload)
                elif kind == "fail":
                    set_busy(False)
                    messagebox.showerror("出错了", payload)
        except queue.Empty:
            pass
        root.after(80, drain)

    def handle_event(ev):
        stage = STAGE_CN.get(ev.get("stage") or "", ev.get("stage") or "")
        idx, total = ev.get("index"), ev.get("total")
        if idx and total:
            prog.configure(mode="determinate", maximum=total, value=idx)
            note = f"{stage} {idx}/{total}"
        else:
            note = stage
        level = ev.get("level")
        if level == "warning":
            log(f"⚠ {ev.get('message', '')}", "warn")
        elif level == "error":
            log(f"✗ {ev.get('message', '')}", "err")
        code = ev.get("code") or ""
        # 只翻译契约里点名的 code，其余不猜
        if code == "session.backup_saved":
            fields = ev.get("fields") or {}
            log(f"已保存备份：{Path(fields.get('file') or '').name}（{fields.get('bytes')} 字节）", "ok")
        if app["busy"]:
            banner.configure(text=f"● 正在{note}…", foreground="#a05000")

    def handle_done(tag, result, stderr):
        set_busy(False)
        if result is None:
            set_banner("● 进程异常退出", "#b00020")
            log(f"EWR 没有返回结果行。stderr: {stderr[:400] or '空'}", "err")
            return
        ok = bool(result.get("ok"))
        code = result.get("error_code")
        data = result.get("data") or {}
        if not ok and code:
            log(f"失败：{ERR_CN.get(code, code)}（{code}）", "err")
            if result.get("error"):
                log(f"   原始信息：{result['error']}", "dim")
        if tag == "list":
            do_list_done(ok, data, code)
        elif tag == "status":
            do_status_done(ok, data, code)
        elif tag == "info":
            do_info_done(ok, data)
        elif tag == "dryrun":
            do_dryrun_done(ok, data, code)
        elif tag == "reset":
            do_reset_done(ok, result, data, code)
        elif tag == "restore":
            do_restore_done(ok, data, code)
        elif tag == "maint":
            do_maint_done(ok, data, code)
        elif tag == "dump":
            do_dump_done(ok, data, code)

    # ---------------- 诊断子进程
    def handle_line(tag, line):
        if "结论：" in line:
            log(line, "warn")
            set_banner(f"● {line}", "#a05000")
        elif line.startswith("下一步") or line.startswith("依据"):
            log(line, "ok")
        else:
            log(line, "")

    def handle_rundone(tag, code, err):
        set_busy(False)
        if err:
            log(f"诊断进程出错：{err}", "err")
        if code == 0:
            set_banner("● 诊断完成：墨水确实动了", "#0a6b0a")
        elif code == 3:
            set_banner("● 诊断完成：看结论，多半要走物理排查", "#a05000")
        else:
            set_banner("● 诊断中断", "#b00020")
        if REPORT_FILE.exists():
            log(f"诊断报告：{REPORT_FILE}", "dim")
            if messagebox.askyesno("诊断结束", f"报告已存到：\n{REPORT_FILE}\n\n现在打开看吗？"):
                os.startfile(str(REPORT_FILE))  # noqa: S606

    # ---------------- 各命令收尾
    def do_list_done(ok, data, code):
        ifaces = data.get("interfaces") or []
        nets = data.get("network_printers") or []
        if ok and (ifaces or nets):
            parts = []
            for i in ifaces:
                parts.append(f"USB {i.get('class')}" + (f"（识别为 {i.get('model_match')}）" if i.get("model_match") else ""))
            for n in nets:
                parts.append(f"网络 {n.get('address')}" + (f"（{n.get('reported_model')}）" if n.get("reported_model") else ""))
            log("检测到：" + "；".join(parts), "ok")
            set_banner("● 已连上打印机，正在读取计数…", "#0a6b0a")
            ewr.start(["--status"], "status")
        else:
            set_banner("● 未检测到打印机", "#b00020")
            log("没有应答的 Epson 设备。常见原因：USB 线只充电不通数据、打印机电源没开、"
                "驱动占着接口（关掉其他打印机软件再试）、或是用了 USB 集线器。", "warn")
            messagebox.showwarning(
                "没找到打印机",
                "没检测到打印机。\n\n请确认：\n"
                "1) 打印机的电源打开（面板亮着）\n"
                "2) USB 线直接插电脑，别走集线器／别插前面板\n"
                "3) 关掉爱普生官方软件、其他清零工具\n\n"
                "弄好后点「检测打印机」重试。",
            )

    def do_status_done(ok, data, code):
        if not ok:
            if code == "model_required":
                log("打印机没自报型号，右上角把型号选成 L3250 再点「检测打印机」。", "warn")
                set_banner("● 识别不出型号，请手动选 L3250", "#b00020")
            else:
                set_banner("● 读取失败", "#b00020")
            return
        app["last_status"] = data
        model = data.get("model") or "?"
        pr = data.get("printer") or {}
        fw = pr.get("firmware") or ""
        lbl_model.configure(text=f"型号 {model}")
        lbl_state.configure(text=f"状态 {pr.get('state') or '?'}")
        pads = data.get("pads") or []
        tree.delete(*tree.get_children())
        worst = -1
        for p in pads:
            pct = p.get("percent")
            used, mx = p.get("used"), p.get("max")
            tags = ()
            if isinstance(pct, (int, float)):
                worst = max(worst, pct)
                if pct >= 85:
                    tags = ("danger",)
            tree.insert(
                "",
                "end",
                values=(p.get("name") or "?", used if used is not None else "-",
                        mx if mx is not None else "-",
                        f"{pct}%" if pct is not None else "无上限"),
                tags=tags,
            )
        total = data.get("pads_total")
        if not pads:
            log(f"这台机器报出 {total} 个计数器，但一个都没读全——数据不可靠。", "warn")
        ec = pr.get("error_code")
        if ec in BLOCKER_PAD_CODES:
            set_banner(f"● 废墨垫已满：{BLOCKER_PAD_CODES[ec]}", "#b00020")
            log(f"打印机自己报出废墨满错误（error_code={ec}），这就是要清零的对象。", "err")
        elif worst >= 85:
            set_banner(f"● 废墨垫接近上限（最高 {worst}%）", "#b06000")
        elif worst >= 0:
            set_banner(f"● 就绪（最高占用 {worst}%）", "#0a6b0a")
        else:
            set_banner("● 已连上，但这些计数器没有上限值", "#0a6b0a")
        log(f"读取完成：{model}，{len(pads)} 个计数器", "ok")
        ewr.start(["--info"], "info")

    def do_info_done(ok, data):
        if not ok:
            return
        fw = data.get("firmware") or "未报"
        sn = data.get("serial") or "未报"
        lbl_info.configure(text=f"固件 {fw}    序列号 {sn}")
        totals = {c.get("key"): c.get("value") for c in (data.get("counters") or [])}
        if totals.get("total_print_page_counter") is not None:
            log(f"累计打印 {totals['total_print_page_counter']} 页", "dim")

    def do_dryrun_done(ok, data, code):
        plan = data.get("planned_writes") or []
        if not ok and code != "read_failed":
            set_banner("● 预演失败", "#b00020")
            return
        if not plan:
            messagebox.showerror("型号未支持", "型号库里这个型号还没有可写的复位地址，不能清零。")
            return
        open_reset_dialog(plan)

    def do_reset_done(ok, result, data, code):
        w = data.get("writes") or {}
        v = data.get("verification") or {}
        if ok:
            set_banner("● 清零成功——请用电源键关机再开机", "#0a6b0a")
            log(f"写入完成并回读确认：{w.get('verified')}/{w.get('total')} 字节", "ok")
            bk = data.get("backup") or {}
            if bk.get("file"):
                log(f"改动前的值已备份到 {Path(bk['file']).name}", "dim")
            messagebox.showinfo(
                "清零成功",
                f"废墨计数器已写回初始值，{w.get('verified')}/{w.get('total')} 字节回读确认。\n\n"
                "最后一步很重要：\n"
                "按打印机上的电源键关机，等 5 秒，再开机。\n"
                "只拔 USB 或断电没用，计数器要在开机的初始化里才生效。",
            )
            ewr.start(["--status"], "status")
        elif code == "write_unverified":
            set_banner("● 写了但没生效", "#b00020")
            log(f"回读对不上：不符 {v.get('mismatches')} 字节，其中 {v.get('unchanged')} 字节仍是写入前的值。"
                "这台机器的写关键字可能不对，或写入被拒绝了。", "err")
            messagebox.showerror(
                "写入没有生效",
                "打印机应答了，但数据没变。\n\n"
                "别反复重试，先看清楚：\n"
                "· 确认接的是 USB 直连（L3250 有扫描仪，接口容易认错）\n"
                "· 关掉所有占用打印机的软件\n"
                "· 换一根确定能传数据的 USB 线\n\n"
                "如果备份还在，随时可以点「还原备份」退回去。",
            )
        elif code == "blocked":
            set_banner("● 被安全闸拦住，未写入", "#b00020")
            messagebox.showwarning("被拦住了", "打印机当前状态不允许写入（可能是报错或忙），什么都没动。")
        else:
            set_banner("● 清零失败", "#b00020")

    def do_restore_done(ok, data, code):
        w = data.get("writes") or {}
        if ok:
            log(f"还原完成：{w.get('verified')}/{w.get('total')} 字节确认", "ok")
            set_banner("● 已还原——请关机再开机", "#0a6b0a")
            messagebox.showinfo("已还原", "备份里的值已经写回去了。\n请用电源键关机再开机让它生效。")
            ewr.start(["--status"], "status")
        else:
            set_banner("● 还原失败", "#b00020")

    def do_maint_done(ok, data, code):
        """这里以前只写一行「维护完成/失败」，等于把打印机说的话整个吞掉了。"""
        secs = data.get("seconds")
        phase = str(data.get("phase") or "")
        route = data.get("route") or "?"
        started = data.get("started")

        if ok:
            log(f"打印机回报：phase={phase or '?'}  route={route}  开始动了={started}  用时 {secs}s", "ok")
            before = {p.get("name"): p.get("used") for p in (data.get("pads_before") or []) if isinstance(p, dict)}
            after = {p.get("name"): p.get("used") for p in (data.get("pads_after") or []) if isinstance(p, dict)}
            moved = False
            for name in before:
                if name in after and isinstance(before[name], (int, float)) and isinstance(after[name], (int, float)):
                    d = after[name] - before[name]
                    log(f"  废墨垫 {name}：{before[name]} -> {after[name]}（{d:+d}）", "dim")
                    moved = moved or d > 0
            if phase in ("never_started", "not_started"):
                set_banner("● 打印机没动（只是答应了）", "#a05000")
                log("打印机收下了指令，但状态一直停在待机。换「打印数据」通道再试，或跑一次清洗诊断。", "warn")
            elif phase == "timed_out":
                set_banner("● 等超时了，打印机可能还在做", "#a05000")
            elif phase == "done" and not moved:
                set_banner("● 命令完成，但废墨垫计数没动", "#a05000")
                log("说明墨水很可能没被抽进废墨垫。点「清洗诊断」跑一遍，会把前后计数和结论一起给你。", "warn")
            else:
                set_banner("● 维护完成", "#0a6b0a")
                if phase == "done":
                    log("墨水流过废墨垫了。断线没好就再来一次；别连着做第三轮，容易进空气。", "dim")
            return

        # 失败：把打印机/EWR 的原话放出来，别只说「失败了」
        set_banner(f"● 维护失败：{ERR_CN.get(code, code or '未知')}", "#b00020")
        log(f"维护失败 code={code}：{ERR_CN.get(code, code or '')}", "err")
        if data.get("phase"):
            log(f"  phase={data['phase']}  route={data.get('route')}", "dim")
        if data.get("explanation"):
            log(f"  打印机说明：{data['explanation']}", "warn")
        err_code = data.get("error_code")
        if err_code in (16, 44):
            log("这是废墨垫计数满（SERVICE REQUEST / CARTRIDGE OVERFLOW）。先用「一键清零」"
                "把计数写回初值，再拔电源 30 秒重开机，然后才能清洗。", "warn")
        elif err_code == 5:
            log("墨仓见底了。清洗只会往喷头里抽空气，先把墨加到 1/3 以上。", "warn")
        elif code == "blocked":
            log("打印机处在报错状态，维护指令根本没发出去。先消掉面板上的报错。", "warn")
        elif code == "not_started":
            log("打印机接受了指令却没启动。试试底部把通道切成「打印数据」再点一次。", "warn")
        if data.get("overrides"):
            log(f"  overrides={data['overrides']}", "dim")

    def do_dump_done(ok, data, code):
        if ok:
            log(f"已保存完整转储：{Path(data.get('file') or '').name}", "ok")
        set_banner("● 转储完成" if ok else "● 转储失败", "#0a6b0a" if ok else "#b00020")

    # ---------------- 按钮行为
    def chosen_model():
        m = cmb_model.get()
        return None if m.startswith("自动") else m

    def args_with_model(extra):
        m = chosen_model()
        return extra + (["--model", m] if m else [])

    def act_detect():
        set_busy(True, "检测打印机")
        log("开始检测打印机…", "dim")
        ewr.start(args_with_model(["--list"]), "list")

    def act_read():
        set_busy(True, "读取计数器")
        log("读取废墨计数器…", "dim")
        ewr.start(args_with_model(["--status"]), "status")

    def act_reset():
        if not app["last_status"]:
            if not messagebox.askyesno("还没读过计数器", "还没成功读取过计数器。要先读一次吗？（推荐先读）"):
                return
            act_read()
            return
        set_busy(True, "预演写入内容")
        log("预演：先算一遍要写哪些地址，不动打印机（--dry-run）", "dim")
        ewr.start(args_with_model(["--dry-run"]), "dryrun")

    def open_reset_dialog(plan):
        dlg = tk.Toplevel(root)
        dlg.title("确认清零")
        dlg.transient(root)
        dlg.grab_set()
        dlg.resizable(False, False)

        frm = ttk.Frame(dlg, padding=18)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="即将写入打印机主板 EEPROM", font=big_font).pack(anchor="w")
        ttk.Label(
            frm,
            text="下面是这次会改动的字节。写之前会自动备份原值，写之后会回读校验。",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(6, 10))

        box = tk.Text(frm, height=9, width=62, font=("Consolas", 9), relief="solid", borderwidth=1)
        box.pack(fill="x")
        # 当前值只能从刚读到的 status 里取，取不到就老实写「—」，不编
        current = {c.get("address"): c.get("value") for c in ((app["last_status"] or {}).get("counters") or [])}
        lines = [f"{'地址':>6}  {'当前值':>8}  {'写入值':>8}"]
        lines.append("-" * 30)
        for p in plan:
            addr = p.get("address")
            cur = current.get(addr)
            lines.append(f"{addr:>6}  {str(cur) if cur is not None else '—':>8}  {str(p.get('value')):>8}")
        box.insert("1.0", "\n".join(lines))
        box.configure(state="disabled")

        warn = ttk.Label(
            frm,
            text="清零只是把计数器归零，不会把废墨垫里的墨水抽走。\n"
                 "如果垫子真的吸满了，继续打印会漏墨、弄脏机器甚至流到桌面上。",
            foreground="#b00020",
            wraplength=520,
            justify="left",
        )
        warn.pack(anchor="w", pady=(12, 8))

        v1 = tk.BooleanVar(value=False)
        v2 = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="我确认过废墨垫没有真的吸满（或已更换/已加装外接瓶）", variable=v1).pack(anchor="w")
        ttk.Checkbutton(frm, text="我知道这是主板级的写入，接错机器可能报废", variable=v2).pack(anchor="w", pady=(4, 0))

        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(14, 0))

        go = ttk.Button(row, text="确认清墨", style="Go.TButton")
        go.pack(side="right")
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="right", padx=(0, 8))

        def refresh(*_):
            go.configure(state="normal" if (v1.get() and v2.get()) else "disabled")

        v1.trace_add("write", refresh)
        v2.trace_add("write", refresh)
        refresh()

        def do_go():
            dlg.destroy()
            log("=== 开始清零 ===", "ok")
            set_busy(True, "写入废墨计数器（别断电）")
            ewr.start(args_with_model(["--yes"]), "reset")

        go.configure(command=do_go)
        dlg.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - dlg.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

    def act_restore():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(BACKUP_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            messagebox.showinfo("没有备份", f"备份目录还是空的：\n{BACKUP_DIR}\n\n清零成功后这里会出现备份文件。")
            return
        path = filedialog.askopenfilename(
            title="选择要还原的备份",
            initialdir=str(BACKUP_DIR),
            filetypes=[("EWR 备份", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        if not messagebox.askyesno(
            "确认还原",
            f"把这份备份的值写回打印机？\n\n{Path(path).name}\n\n"
            "还原会让计数器回到备份时的数字——如果备份是在清零后做的，等于把「满」写回去。",
            icon="warning",
        ):
            return
        set_busy(True, "还原备份")
        log(f"还原：{Path(path).name}", "dim")
        ewr.start(["--restore", path, "--yes"], "restore")

    def act_dump():
        if not messagebox.askyesno(
            "完整备份",
            "把整块 EEPROM（2048 字节）读出来存成文件？\n\n"
            "这是最彻底的保险：万一以后想完全恢复，有它就能还原。约需十几秒。",
        ):
            return
        set_busy(True, "读取整块 EEPROM")
        ewr.start(args_with_model(["--dump"]), "dump")

    def channel_args():
        return ["--print-data"] if cmb_channel.get().startswith("打印数据") else []

    def act_maint(kind, extra, label):
        tip = {
            "nozzle-check": "打印机会打一张喷嘴检查图，用纸。",
            "head-clean": "会用掉一点墨。太频繁连续清洗反而容易进空气。",
            "power-clean": "强力充墨，很费墨，通常只在长时间没用、断线严重时用一次。",
        }[kind]
        # 先把「这一下注定白做」的情况挑出来，省一轮墨和时间
        st = app["last_status"] or {}
        pr = st.get("printer") or {}
        ec = pr.get("error_code")
        if ec in (16, 44):
            if messagebox.askyesno("废墨垫计数满了",
                                   "打印机现在把自己锁着（废墨垫计数满），维护指令会被直接拒绝。\n\n"
                                   "先去「一键清零」把计数写回初值、拔电源 30 秒重开机。现在还要试吗？"):
                pass
            else:
                return
        inks = [i.get("level") for i in (pr.get("inks") or []) if isinstance(i.get("level"), (int, float))]
        if inks and min(inks) <= 33:
            log(f"墨量偏低（最低 {min(inks)}%）：清洗抽不上墨，只会把空气吸进喷头。", "warn")
        if not messagebox.askyesno("确认", f"{label}？\n\n{tip}\n\n打印机现在必须空闲（没有卡纸/报错）。"):
            return
        set_busy(True, label)
        log(f"{label}{'（打印数据通道）' if channel_args() else ''}…", "dim")
        ewr.start(args_with_model([f"--{kind}"] + extra + channel_args()), "maint")

    def act_diag():
        """清洗诊断：把「点了清洗没反应」变成一条能读的结论。"""
        if not DIAG.exists():
            messagebox.showerror("缺文件", f"找不到诊断脚本：\n{DIAG}\n\n工具目录不完整。")
            return
        cmd = [sys.executable, str(DIAG), "--level", "normal"]
        m = chosen_model()
        if m:
            cmd += ["--model", m]
        cmd += channel_args() + ["--out", str(REPORT_FILE)]
        log("=== 清洗诊断：清洗前后各读一次 EEPROM 计数，比较差值 ===", "ok")
        log("约 2 分钟；期间别关窗口、别拔 USB。", "dim")
        set_busy(True, "清洗诊断")
        set_banner("● 正在诊断（发送清洗 + 前后测量）…", "#a05000")
        runner.start(cmd, "diag")

    def open_backup_dir():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(BACKUP_DIR))  # noqa: S606

    def open_log():
        if LOG_FILE.exists():
            os.startfile(str(LOG_FILE))  # noqa: S606
        else:
            messagebox.showinfo("没有日志", "还没产生日志。")

    def open_report():
        if REPORT_FILE.exists():
            os.startfile(str(REPORT_FILE))  # noqa: S606
        else:
            messagebox.showinfo("还没有报告", "先点「清洗诊断」跑一次，结论会写到 诊断报告.txt。")

    # ---------------- 布局
    header = ttk.Frame(root, padding=(14, 12, 14, 6))
    header.pack(fill="x")
    ttk.Label(header, text="Epson L3250 清墨器 + 清洗诊断", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
    ttk.Label(header, text="写主板废墨计数器 · 不碰固件", foreground="#777").pack(side="left", padx=(10, 0), pady=(6, 0))

    banner = tk.Label(root, text="● 未连接", font=big_font, anchor="w", fg="#666", bg="#f3f4f6", padx=14, pady=8)
    banner.pack(fill="x", padx=14)

    prog = ttk.Progressbar(root, mode="determinate", maximum=100)
    prog.pack(fill="x", padx=14, pady=(0, 8))

    # 信息条
    info = ttk.Frame(root, padding=(14, 0))
    info.pack(fill="x")
    lbl_model = ttk.Label(info, text="型号 —", font=bold_font)
    lbl_model.pack(side="left")
    lbl_state = ttk.Label(info, text="状态 —", padding=(18, 0, 0, 0))
    lbl_state.pack(side="left")
    lbl_info = ttk.Label(info, text="固件 —    序列号 —", foreground="#555")
    lbl_info.pack(side="left", padx=(18, 0))

    # 计数器表
    ttk.Label(root, text="废墨计数器", font=bold_font, padding=(14, 10, 0, 4)).pack(anchor="w")
    table_frame = ttk.Frame(root, padding=(14, 0))
    table_frame.pack(fill="x")
    tree = ttk.Treeview(table_frame, columns=("name", "used", "max", "pct"), show="headings", height=5)
    for col, title, w, anchor in (
        ("name", "计数器", 300, "w"),
        ("used", "已用", 90, "e"),
        ("max", "上限", 90, "e"),
        ("pct", "占用", 110, "e"),
    ):
        tree.heading(col, text=title)
        tree.column(col, width=w, anchor=anchor)
    tree.tag_configure("danger", foreground="#c00000")
    tree.pack(fill="x")
    ttk.Label(
        root,
        text="占用接近或超过 100% 时，打印机通常会亮红灯并报「需要服务」。清零只是让它重新记数。",
        foreground="#777",
        padding=(14, 4, 14, 0),
    ).pack(anchor="w")

    # 按钮区
    bar = ttk.Frame(root, padding=(14, 12, 14, 6))
    bar.pack(fill="x")

    b_detect = ttk.Button(bar, text="① 检测打印机", command=act_detect, width=16)
    b_read = ttk.Button(bar, text="② 读取计数", command=act_read, width=14)
    b_reset = ttk.Button(bar, text="③ 一键清零", command=act_reset, style="Go.TButton", width=14)
    b_restore = ttk.Button(bar, text="还原备份", command=act_restore, width=12)
    b_dump = ttk.Button(bar, text="完整备份", command=act_dump, width=12)
    for b in (b_detect, b_read, b_reset, b_restore, b_dump):
        b.pack(side="left", padx=(0, 8))
    buttons = [b_detect, b_read, b_reset, b_restore, b_dump]

    # 维护区
    mnt = ttk.Frame(root, padding=(14, 0))
    mnt.pack(fill="x")
    ttk.Label(mnt, text="维护：").pack(side="left")
    for kind, extra, label, w in (
        ("nozzle-check", [], "喷嘴检查", 12),
        ("head-clean", ["gentle"], "轻度清洗", 12),
        ("head-clean", ["normal"], "正常清洗", 12),
        ("head-clean", ["strong"], "强力清洗", 12),
        ("power-clean", [], "强力充墨", 12),
    ):
        btn = ttk.Button(mnt, text=label, width=w, command=lambda k=kind, e=extra, l=label: act_maint(k, e, l))
        btn.pack(side="left", padx=(0, 6))
        buttons.append(btn)

    ttk.Separator(mnt, orient="vertical").pack(side="left", fill="y", padx=(6, 8))
    b_diag = ttk.Button(mnt, text="清洗诊断（先点这个）", width=20, command=act_diag, style="Go.TButton")
    b_diag.pack(side="left")
    buttons.append(b_diag)

    # 型号 + 工具
    foot = ttk.Frame(root, padding=(14, 8, 14, 4))
    foot.pack(fill="x")
    ttk.Label(foot, text="清洗通道：").pack(side="left")
    cmb_channel = ttk.Combobox(foot, values=["自动（打印机指令）", "打印数据（备用）"], width=17, state="readonly")
    cmb_channel.set("自动（打印机指令）")
    cmb_channel.pack(side="left", padx=(0, 10))
    ttk.Label(foot, text="型号：").pack(side="left")
    cmb_model = ttk.Combobox(foot, values=["自动识别", "L3250"], width=12, state="readonly")
    cmb_model.set("自动识别")
    cmb_model.pack(side="left")
    ttk.Button(foot, text="打开诊断报告", command=open_report).pack(side="left", padx=(14, 6))
    ttk.Button(foot, text="打开备份文件夹", command=open_backup_dir).pack(side="left", padx=(0, 6))
    ttk.Button(foot, text="打开日志", command=open_log).pack(side="left")

    # 日志
    ttk.Label(root, text="运行日志", font=bold_font, padding=(14, 6, 0, 2)).pack(anchor="w")
    log_frame = ttk.Frame(root, padding=(14, 0, 14, 12))
    log_frame.pack(fill="both", expand=True)
    txt = tk.Text(log_frame, height=8, wrap="word", font=("Consolas", 9), relief="solid", borderwidth=1,
                  bg="#fbfbfb")
    sb = ttk.Scrollbar(log_frame, command=txt.yview)
    txt.configure(yscrollcommand=sb.set, state="disabled")
    txt.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    txt.tag_configure("ok", foreground="#0a6b0a")
    txt.tag_configure("err", foreground="#b00020")
    txt.tag_configure("warn", foreground="#a05000")
    txt.tag_configure("dim", foreground="#888")

    log("清墨器启动。EWR 版本见下一条。", "dim")
    root.after(80, drain)
    root.after(400, act_detect)
    root.mainloop()


def main():
    if "--check" in sys.argv or "--selftest" in sys.argv:
        return selftest()
    if not EWR.exists():
        try:
            import tkinter.messagebox as mb
            import tkinter as tk

            r = tk.Tk()
            r.withdraw()
            mb.showerror("缺文件", f"找不到 EWR 目录里的 ewr.exe：\n{EWR}\n\n工具目录不完整，请重新解压。")
            r.destroy()
        except Exception:  # noqa: BLE001
            print(f"找不到 {EWR}")
        return 1
    main_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
