# -*- coding: utf-8 -*-
"""
Epson L3250 清洗诊断器 / cleaning diagnostic

它做的不是「再点一次清洗」，而是回答一个具体问题：
这次清洗，墨水到底有没有被抽进废墨垫？

方法：在清洗前后各读一次 EEPROM 计数，比较差值。
  - 计数涨了  -> 墨水确实流动过，问题在喷头/管路（物理层）
  - 计数没动  -> 打印机只是回了个「完成」，泵没工作（或这台机器不记外部指令）
  - 命令被拦  -> 打印机处在报错态（废墨垫满 / 缺墨 / 忙），根本没开始

底层调用 EWR v1.6.0 的 --json 契约（github.com/RxNaison/Epson-Waste-Reset, Apache-2.0）。

用法：
  python l3250_diag.py                 # 默认：正常清洗 + 前后测量
  python l3250_diag.py --level strong
  python l3250_diag.py --print-data    # 打印机不认自家维护指令时改走打印数据
  python l3250_diag.py --out 报告.txt   # 另存报告，并写一份 .json
  python l3250_diag.py --selftest      # 不用打印机，验证判定逻辑
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CREATE_NO_WINDOW = 0x08000000

LEVEL_CN = {"gentle": "轻度清洗", "normal": "正常清洗", "medium": "中等清洗", "strong": "强力清洗", "power": "强力充墨"}
GROUP_CN = {"all": "全部喷头", "black": "黑色", "color": "彩色"}

# EWR 的 error_code（契约稳定枚举）-> 人话
ERR_CN = {
    "blocked": "被打印机自身的报错拦住了，指令没有发出去",
    "send_failed": "指令发出去了，打印机回的不是 OK",
    "not_started": "打印机收到了指令，但一直没离开待机状态",
    "printer_error": "打印机在执行中报错",
    "timed_out": "等待超时（打印机会自己把这一轮做完）",
    "not_supported": "这个型号/连接方式下不支持该指令",
    "device_not_found": "没找到打印机",
    "model_required": "识别不出型号，需要 --model L3250",
    "another_run": "另一个 ewr 进程正占着打印机",
    "bad_usage": "参数不对",
}

# 打印机报错码（state.error_code）
PRINTER_ERR_CN = {
    16: "SERVICE REQUEST（废墨垫计数到期）",
    44: "CARTRIDGE OVERFLOW（废墨溢出）",
    5: "INK OUT（缺墨）",
    6: "PAPER OUT（缺纸）",
}


def now() -> str:
    return time.strftime("%H:%M:%S")


# --------------------------------------------------------------------------- 子进程
def find_ewr() -> Path | None:
    env = os.environ.get("L3250_EWR")
    cands = []
    if env:
        cands.append(Path(env))
    cands += [HERE / "EWR" / "ewr.exe", HERE / "ewr.exe", HERE.parent / "EWR" / "ewr.exe"]
    for c in cands:
        if c.is_file():
            return c
    return None


class Interrupted(Exception):
    pass


def run_ewr(ewr: Path, args: list[str], timeout: float, say) -> dict:
    """跑一次 ewr，逐行解析 JSON。返回最后一个 type=result 的对象（或 {}）。"""
    cmd = [str(ewr)] + args + ["--json", "--no-update"]
    proc = subprocess.Popen(
        cmd,
        cwd=str(ewr.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    killed = {"hit": False}

    def watchdog():
        if not killed["hit"]:
            killed["hit"] = True
            try:
                proc.kill()
            except Exception:
                pass

    timer = threading.Timer(timeout, watchdog)
    timer.daemon = True
    timer.start()

    result = {}
    stderr_buf = []
    err_thread = threading.Thread(target=lambda: stderr_buf.extend(proc.stderr.read() or []), daemon=True)
    err_thread.start()

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = obj.get("type")
            if kind == "event":
                note = obj.get("message") or obj.get("stage") or ""
                if note:
                    say(f"    · {note}")
            elif kind == "result":
                result = obj
    finally:
        timer.cancel()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
        err_thread.join(timeout=2)

    if killed["hit"] and not result:
        result = {"ok": False, "error_code": "timed_out", "error": "本地等待超时，已强制结束"}
    return result


# --------------------------------------------------------------------------- 解析
def as_list(obj):
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        return [{"name": k, "used": v} for k, v in obj.items()]
    return []


def pad_used(pads) -> dict:
    """{垫名: 已用计数}，只要原始 used，不用 percent（取整会掩盖一轮清洗）。"""
    out = {}
    for p in as_list(pads):
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "?")
        used = p.get("used")
        if isinstance(used, (int, float)):
            out[name] = used
    return out


def counters(result: dict) -> dict:
    data = (result or {}).get("data") or {}
    out = {}
    for c in as_list(data.get("counters")):
        if isinstance(c, dict) and c.get("key") is not None:
            out[str(c["key"])] = c.get("value")
    return out


CLEAN_KEYS = ("manual_cleaning_counter", "power_cleaning_counter", "timer_cleaning_counter")


# --------------------------------------------------------------------------- 判定
class Verdict:
    def __init__(self, code, title, basis, todo):
        self.code = code
        self.title = title
        self.basis = basis      # 依据（数字）
        self.todo = todo        # 下一步

    def as_dict(self):
        return {"code": self.code, "title": self.title, "basis": self.basis, "todo": self.todo}


PHYSICAL = [
    "掀开上盖看墨管（喷头左上方那几根透明管）：里面有气泡或空段，说明墨水根本没到喷头，清洗再多次也没用。",
    "拔下废墨管（废墨垫那一段软管），接注射器缓慢回抽 2–3 ml，同时让打印机做一次清洗；看有没有墨出来。抽完立刻插回。",
    "把喷头移出，用喷头清洗液或温水浸湿无纺布/纸巾叠厚，垫在帽站位置，再让喷头压上去闷 20–30 分钟。",
    "棉签蘸清洗液擦帽站的橡胶密封圈和刮片，擦完吸干——别让液体流进机器。",
    "做完以上任一项，关机静置 8–12 小时，再做一次 power clean（强力充墨），然后关机静置 12 小时。",
    "全部无效，基本是喷头干堵报废或墨泵组件损坏，该送修了。",
]


def analyze(trace: dict) -> Verdict:
    """trace = {status, info_before, maint, info_after}，每个都是一次 ewr 的 result。"""
    status = trace.get("status") or {}
    maint = trace.get("maint") or {}
    sdata = status.get("data") or {}
    printer = sdata.get("printer") or {}

    # 1) 连不上 / 型号不明：后面全是空谈
    for name, res in (("status", status), ("maint", maint)):
        code = (res or {}).get("error_code")
        if code in ("device_not_found", "another_run", "bad_usage", "model_required"):
            return Verdict("link", "没法和打印机正常对话", [f"{name} 返回 {code}：{ERR_CN.get(code, code)}"], [
                "确认打印机开机、USB 线直插电脑（别走集线器）、关掉爱普生官方软件和其他清零工具。",
                "还报 model_required 就加参数 --model L3250。",
            ])

    # 2) 打印机在报错态：EWR 连发都不会发
    perr = printer.get("error_code")
    if maint and not maint.get("ok") and maint.get("error_code") == "blocked":
        mdata = maint.get("data") or {}
        expl = mdata.get("explanation") or maint.get("error") or ""
        pcode = mdata.get("error_code") or perr
        basis = [f"打印机会拒绝一切维护指令，原因是 {PRINTER_ERR_CN.get(pcode, str(pcode))}"]
        if expl:
            basis.append(f"EWR 原文：{expl}")
        if pcode in (16, 44):
            return Verdict("pads_full", "废墨垫计数满了，打印机把自己锁住了", basis, [
                "先用本工具的「清零废墨计数」把计数写回初值（写前会自动备份）。",
                "清零后拔电源 30 秒再开机——不重启的话打印机的报错状态不会消。",
                "然后回来重跑这条诊断。垫子物理上真的满到漏墨，清完也该换/加吸墨棉。",
            ])
        if pcode == 5:
            return Verdict("ink_low", "墨仓见底，清洗只会往喷头里抽空气", basis, [
                "把四个墨仓都加到 1/3 以上再清洗。",
                "加完墨先静置 10 分钟，再做 power clean 把管路里的空气顶出去。",
            ])
        return Verdict("printer_error", "打印机在报错，维护指令被拦", basis, [
            "先把打印机面板上那个报错消掉（缺纸、纸张卡住、盖子没关之类），再重跑。",
            f"如果面板没显示任何异常，可能是 {pcode} 这类只在数据里出现的错误，把本报告贴到仓库 issue。",
        ])
    if printer.get("error"):
        pcode = perr
        return Verdict("printer_error", f"打印机处于错误状态：{PRINTER_ERR_CN.get(pcode, printer.get('error'))}", [
            f"state={printer.get('state')} error={printer.get('error')}",
        ], ["先消掉面板报错再重跑。"])

    # 3) 清洗本身的结果
    phase = str((maint.get("data") or {}).get("phase") or "").strip()
    mdata = maint.get("data") or {}
    route = mdata.get("route") or "?"
    started = bool(mdata.get("started"))
    seconds = mdata.get("seconds")

    if not maint.get("ok") and not phase:
        code = maint.get("error_code") or "failed"
        return Verdict("command_failed", f"清洗指令没跑完：{ERR_CN.get(code, code)}",
                       [f"error={maint.get('error')}"],
                       ["把日志贴到 issue；可先试 --print-data 换一条指令通道。"])

    if phase in ("aborted",):
        return Verdict("command_failed", "清洗被中止", [f"phase={phase}"], ["重跑一次；还中止就把报告贴到 issue。"])
    if phase in ("not_supported",):
        return Verdict("command_failed", "这台机器不接受这条清洗指令",
                       [f"phase={phase} route={route}"],
                       ["加 --print-data 再跑一次：改走打印数据通道，绕开打印机自家的维护指令。"])
    if phase in ("send_failed", "error", "device_not_found"):
        return Verdict("command_failed", f"指令没送进去（{phase}）",
                       [f"phase={phase} reply={mdata.get('reply')}"],
                       ["加 --print-data 再跑一次。", "USB 换口、线换一根，别走集线器。"])
    if phase == "never_started":
        return Verdict("ignored", "打印机答应了，但一步都没动",
                       [f"phase=never_started route={route}（指令被接收，状态始终停在待机）"],
                       ["加 --print-data 重发一次；L3250 认自家维护指令，但个别固件版本要绕。",
                        "还不动就用面板法：按住停止键 5 秒，电源灯开始闪，等约 3 分钟。",
                        "面板法也不动 -> 按下面的物理排查走：多半是泵没吸力。"])
    if phase == "timed_out":
        return Verdict("timeout", "等超时了，但打印机可能还在做",
                       [f"phase=timed_out started={started} seconds={seconds}"],
                       ["等 10 分钟，看面板灯还在闪不闪；闪就是在做，别拔电。",
                        "灯不闪了再重跑这条诊断，看计数有没有涨。"])

    # 4) phase=done：真正的问题是「墨动没动」
    before = pad_used(mdata.get("pads_before"))
    after = pad_used(mdata.get("pads_after"))
    cb = counters(trace.get("info_before"))
    ca = counters(trace.get("info_after"))

    pad_delta = {k: after[k] - before[k] for k in before if k in after}
    cnt_delta = {k: ca[k] - cb[k] for k in CLEAN_KEYS if isinstance(cb.get(k), (int, float))
                 and isinstance(ca.get(k), (int, float))}

    basis = [f"phase={phase} route={route} started={started} 用时 {seconds}s"]
    if pad_delta:
        basis += [f"废墨垫 {k}：{before[k]} -> {after[k]}（{v:+d}）" for k, v in pad_delta.items()]
    if cnt_delta:
        basis += [f"{k}：{cb[k]} -> {ca[k]}（{v:+d}）" for k, v in cnt_delta.items()]

    pads_moved = any(v > 0 for v in pad_delta.values())
    cnt_moved = any(v > 0 for v in cnt_delta.values())

    if not pad_delta and not cnt_delta:
        return Verdict("done_no_meter", "打印机说做完了，但没有任何计数可对照",
                       basis + ["这台机器读不出清洗计数，没法从数据上判断墨有没有动"],
                       ["打一张喷嘴检查图，看断线有没有变。变好=洗到了；一点没变=按下面的物理排查来。"])

    if pads_moved or cnt_moved:
        return Verdict("done_with_ink", "清洗确实执行了，墨水进了废墨垫",
                       basis + ["结论：这次冲洗是真的做了，不是空转。"],
                       ["打喷嘴检查图：断线明显变少 -> 再来 1 次正常清洗就好，别贪多。",
                        "一点没变 -> 墨水到了帽站却没进喷头，属于喷头干堵或管路进气，按下面的物理排查来。"])

    return Verdict("done_no_ink", "打印机回报完成，但没有一滴墨水的痕迹",
                   basis + ["结论：泵很可能没吸力，或者这台的计数器不因外部指令而增加。"],
                   ["先看墨管有没有气泡——有，就是气堵，按物理排查第 2 条直接抽。",
                    "再按下面的物理排查逐条来；重点查帽站密封（橡胶圈老化压不严，泵抽的是空气）。"])


# --------------------------------------------------------------------------- 主流程
def fmt_pads(pads) -> str:
    parts = []
    for p in as_list(pads):
        if not isinstance(p, dict):
            continue
        used, mx, pct = p.get("used"), p.get("max"), p.get("percent")
        if isinstance(pct, (int, float)):
            parts.append(f"{p.get('name')} {pct}%" + (f"（{used}/{mx}）" if used is not None else ""))
        elif used is not None:
            parts.append(f"{p.get('name')} {used}" + (f"/{mx}" if mx else ""))
    return "  ".join(parts) or "（读不到）"


def fmt_inks(status_data) -> str:
    inks = (status_data or {}).get("inks") or []
    parts = []
    for i in inks:
        if not isinstance(i, dict):
            continue
        lv = i.get("level")
        parts.append(f"{i.get('color')} {lv}%" if isinstance(lv, (int, float)) else f"{i.get('color')} ?")
    return "  ".join(parts) or "（读不到墨量）"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Epson L3250 清洗诊断：判断这次清洗到底有没有把墨抽进废墨垫")
    ap.add_argument("--level", default="normal", choices=["gentle", "medium", "normal", "strong", "power"])
    ap.add_argument("--group", default="all", choices=["all", "black", "color"])
    ap.add_argument("--print-data", action="store_true", help="改走打印数据通道（打印机不认自家维护指令时用）")
    ap.add_argument("--model", default="", help="手动指定型号，例如 L3250")
    ap.add_argument("--out", default="", help="报告写到这个文件")
    ap.add_argument("--json", action="store_true", help="把判定结果以 JSON 打到 stdout 最后一行")
    ap.add_argument("--selftest", action="store_true", help="不接打印机，验证判定逻辑")
    ap.add_argument("--timeout", type=float, default=900.0, help="单步最长等待秒数（默认 900）")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if args.selftest:
        return selftest()

    ewr = find_ewr()
    if not ewr:
        print("找不到 EWR\\ewr.exe。先跑 setup-ewr.ps1，或把它放在本脚本旁边的 EWR\\ 目录里。")
        return 1

    lines = []

    def say(text=""):
        print(text)
        sys.stdout.flush()
        lines.append(text)

    base = ["--model", args.model] if args.model else []
    t0 = time.time()

    say(f"Epson L3250 清洗诊断  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    say(f"动作：{LEVEL_CN.get(args.level, args.level)} / {GROUP_CN.get(args.group, args.group)}"
        + (" / 打印数据通道" if args.print_data else " / 打印机自家维护指令"))
    say("")

    say("[1/4] 读取打印机状态 …")
    status = run_ewr(ewr, base + ["--status"], 180, lambda s: None)
    if status.get("ok"):
        sd = status.get("data") or {}
        pr = sd.get("printer") or {}
        say(f"    型号 {sd.get('model') or '?'}  状态 {pr.get('state') or '?'}"
            + (f"  错误 {pr.get('error')}（{pr.get('error_code')}）" if pr.get("error") else "  无报错"))
        say(f"    墨量 {fmt_inks(pr)}")
        say(f"    废墨垫 {fmt_pads(sd.get('pads'))}")
    else:
        say(f"    读取失败：{status.get('error_code')} {status.get('error') or ''}")
    say("")

    say("[2/4] 读取清洗相关计数（清洗前的基线）…")
    info_before = run_ewr(ewr, base + ["--info"], 180, lambda s: None)
    cb = counters(info_before)
    if cb:
        say("    " + "  ".join(f"{k}={cb[k]}" for k in CLEAN_KEYS if cb.get(k) is not None))
    else:
        say(f"    读不到计数：{info_before.get('error_code') or '空'}")
    say("")

    say(f"[3/4] 发送清洗指令（{LEVEL_CN.get(args.level, args.level)}）…")
    say("    正常清洗约 1–3 分钟，强力充墨更久；期间打印机可能很吵，别断电。")
    cmd = ["--head-clean", args.level] if args.level != "power" else ["--power-clean"]
    if args.group != "all":
        cmd += ["--clean-group", args.group]
    if args.print_data:
        cmd += ["--print-data"]
    maint = run_ewr(ewr, base + cmd, args.timeout, say)
    md = maint.get("data") or {}
    if maint.get("ok"):
        say(f"    ok   phase={md.get('phase')}  route={md.get('route')}"
            f"  started={md.get('started')}  {md.get('seconds')}s")
    else:
        say(f"    失败 {maint.get('error_code')}  {maint.get('error') or ''}")
    say("")

    say("[4/4] 清洗后回读计数 …")
    info_after = run_ewr(ewr, base + ["--info"], 180, lambda s: None)
    ca = counters(info_after)
    if ca:
        say("    " + "  ".join(f"{k}={ca[k]}" for k in CLEAN_KEYS if ca.get(k) is not None))
    say("")

    trace = {"status": status, "info_before": info_before, "maint": maint, "info_after": info_after}
    v = analyze(trace)

    say("=" * 62)
    say(f"结论：{v.title}")
    say("")
    say("依据：")
    for b in v.basis:
        say(f"  · {b}")
    say("")
    say("下一步：")
    for i, t in enumerate(v.todo, 1):
        say(f"  {i}. {t}")

    if v.code in ("done_with_ink", "done_no_ink", "done_no_meter", "ignored", "timeout"):
        say("")
        say("物理排查（从上往下做，每做完一条回来看计数有没有变）：")
        for i, t in enumerate(PHYSICAL, 1):
            say(f"  {i}. {t}")
    say("=" * 62)
    say(f"（耗时 {time.time() - t0:.0f}s）")

    report = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "verdict": v.as_dict(),
              "trace": trace, "summary": lines}
    if args.out:
        out = Path(args.out)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {out}")
    if args.json:
        print(json.dumps(report["verdict"], ensure_ascii=False))

    return {"done_with_ink": 0, "done_no_meter": 0}.get(v.code, 3)


# --------------------------------------------------------------------------- 自检
def selftest() -> int:
    S = {"ok": True, "data": {"model": "Epson L3250", "printer": {"state": "IDLE", "error": None},
                              "pads": [{"name": "Main Pad", "used": 700, "max": 6346, "percent": 11}]}}

    def info(m):
        return {"ok": True, "data": {"counters": [{"key": "manual_cleaning_counter", "value": m}]}}

    cases = []

    cases.append(("废墨垫满被拦", {
        "status": S,
        "info_before": info(10),
        "maint": {"ok": False, "error_code": "blocked",
                  "data": {"error": "SERVICE REQUEST", "error_code": 16,
                           "explanation": "The printer will not clean or print while its waste ink pads count as full."}},
        "info_after": info(10),
    }, "pads_full"))

    cases.append(("打印机不动", {
        "status": S, "info_before": info(10),
        "maint": {"ok": True, "data": {"phase": "never_started", "route": "factory", "started": False}},
        "info_after": info(10),
    }, "ignored"))

    cases.append(("真洗了", {
        "status": S, "info_before": info(10),
        "maint": {"ok": True, "data": {"phase": "done", "route": "factory", "started": True, "seconds": 92,
                                       "pads_before": [{"name": "Main Pad", "used": 700}],
                                       "pads_after": [{"name": "Main Pad", "used": 742}]}},
        "info_after": info(11),
    }, "done_with_ink"))

    cases.append(("说完成但零增量", {
        "status": S, "info_before": info(10),
        "maint": {"ok": True, "data": {"phase": "done", "route": "factory", "started": True, "seconds": 61,
                                       "pads_before": [{"name": "Main Pad", "used": 700}],
                                       "pads_after": [{"name": "Main Pad", "used": 700}]}},
        "info_after": info(10),
    }, "done_no_ink"))

    cases.append(("没设备", {
        "status": {"ok": False, "error_code": "device_not_found"},
        "info_before": {}, "maint": {}, "info_after": {},
    }, "link"))

    bad = 0
    for name, trace, want in cases:
        got = analyze(trace).code
        ok = got == want
        bad += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {name}: 期望 {want} 得到 {got}")
    print(f"\n{len(cases) - bad}/{len(cases)} 通过")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
