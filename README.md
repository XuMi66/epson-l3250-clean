# epson-l3250-clean

Head cleaning on an Epson L3250 either works or silently does nothing — and the driver looks exactly the same in both cases. This tool makes the difference measurable.

It does three things:

1. **Measures a cleaning.** Reads the printer's EEPROM counters before and after a cleaning cycle and reports whether any ink actually moved into the waste pad. So "I cleaned it five times and nothing changed" becomes a verdict with numbers.
2. **Tells you why a maintenance command failed.** `phase=never_started` (the printer acknowledged the command but never acted) is a completely different problem from `phase=done` (it ran, and the ink still didn't reach the print head). The Epson driver reports both as "completed".
3. **Reads and resets the waste ink pad counters**, which is what most "cleaning does nothing" cases come down to: once the waste pad counter hits its limit, the printer refuses every maintenance command and stops printing. Writes are backed up beforehand and verified by reading back.

The EEPROM work is done by [EWR](https://github.com/RxNaison/Epson-Waste-Reset) v1.6.0 (Apache-2.0). This project is the diagnostic and GUI layer on top of it, plus the part that explains the result in plain language.

Windows only. Python 3.10+, no third-party packages.

---

## The four things that all look like "cleaning doesn't work"

| # | What is actually happening | How you can tell | What fixes it |
|---|---|---|---|
| A | **Refused.** The printer is in an error state (`0x10 SERVICE REQUEST` / `0x2C CARTRIDGE OVERFLOW` = waste pad counter full, `0x05 INK OUT`, paper out). It never even receives the cleaning command. | Tool reports `blocked`, with the printer's own error code | Reset the waste pad counters, then power-cycle. Refill ink if `0x05`. |
| B | **Accepted, never executed.** The printer replies OK, the command goes in, the state never leaves IDLE. Some firmware revisions won't take their own maintenance command this way. | `phase=never_started` | Retry with `--print-data` (sends the same command down the print-data channel instead). |
| C | **Executed, no ink moved.** Firmware says done. Waste pad counters don't move, cleaning counter doesn't move. The cap station or the ink pump isn't sealing/sucking. | `phase=done` + zero delta | Physical: check tubes for air, pull ink through with a syringe, clean the cap seal. |
| D | **Executed, ink moved, nozzle check still broken.** Ink reached the pad, but not the nozzle plate. | `phase=done` + counters rose | Dried/clogged head or air in the ink path. Soak, then power-clean after a rest. |

Only A and B are software problems. This tool tells you which one you have.

---

## Install

1. Install Python 3.10 or newer (tick "Add python.exe to PATH").
2. Download EWR v1.6.0 and unpack it into an `EWR\` folder next to the scripts:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup-ewr.ps1
   ```

   (Or download [EWR-v1.6.0-win.zip](https://github.com/RxNaison/Epson-Waste-Reset/releases/download/v1.6.0/EWR-v1.6.0-win.zip) yourself and unpack it here; you should end up with `EWR\ewr.exe` and `EWR\database.json`.)
3. Plug the printer in directly — no USB hub, printer switched on, Epson's own software closed.

Sanity check without a printer:

```powershell
python l3250_clean.py --check
```

---

## Use

### GUI

```powershell
python l3250_clean.py
```

`清洗诊断` (cleaning diagnostic) is the button to press first. It runs one cleaning, measures it, and writes `诊断报告.txt`. The GUI itself is in Chinese; the report is plain Chinese text you can paste anywhere.

### Command line

```powershell
# measure a normal cleaning
python l3250_diag.py

# the printer acknowledged the command but never moved
python l3250_diag.py --print-data

# heavier dose, same measurement
python l3250_diag.py --level strong

# self-test the decision logic, no printer needed
python l3250_diag.py --selftest
```

What a real run looks like:

```
[1/4] 读取打印机状态 …
    型号 Epson L3250  状态 IDLE  无报错
    墨量 Black 82%  Cyan 64%  Magenta 71%  Yellow 88%
    废墨垫 Main Pad 11%（700/6346）  Platen 1%（40/3416）

[2/4] 读取清洗相关计数（清洗前的基线）…
    manual_cleaning_counter=10  power_cleaning_counter=2

[3/4] 发送清洗指令（正常清洗）…
    ok   phase=done  route=factory  started=True  92s

[4/4] 清洗后回读计数 …
    manual_cleaning_counter=11  power_cleaning_counter=2

==============================================================
结论：清洗确实执行了，墨水进了废墨垫
依据：
  · 废墨垫 Main Pad：700 -> 742（+42）
  · manual_cleaning_counter：10 -> 11（+1）
```

Exit codes: `0` the cleaning ran and ink moved, `3` diagnosis done but the problem is physical, anything else means the command didn't complete.

---

## Reading the verdict

| Verdict | Meaning | Next step |
|---|---|---|
| `pads_full` | Waste pad counter full, printer locked | Reset counters, unplug 30 s, power on, re-run |
| `ink_low` | Ink below ~1/3 | Refill, wait 10 min, then power-clean |
| `blocked` / `printer_error` | Some other printer error | Clear it on the panel, re-run |
| `ignored` | Command accepted, printer never acted | `--print-data`, or hold the Stop button 5 s |
| `command_failed` | Sent, but rejected or broken off | `--print-data`, different USB port/cable |
| `timeout` | Still running (the printer finishes on its own) | Wait 10 min, don't unplug, then re-measure |
| `done_with_ink` | Cleaning genuinely happened | Nozzle test: better → one more; same → physical |
| `done_no_ink` | "Done" but no counter moved | Pump or cap station: physical checklist |
| `done_no_meter` | Firmware reports nothing to compare | Nozzle test, then physical checklist |

---

## The physical checklist (this is the part that actually unclogs a head)

Work top to bottom. After each step, run the tool again and see whether the counters moved.

1. **Look at the ink tubes.** Open the top cover; the four transparent tubes run to the print head. Bubbles or empty segments mean ink never reached the head — no number of cleanings will change that.
2. **Pull ink through with a syringe.** Unplug the waste tube from the waste pad, attach a syringe, draw 2–3 ml slowly while the printer runs a cleaning. Ink should follow. Reconnect immediately and wipe up.
3. **Soak the head.** Move the head off the cap, lay a folded pad of lint-free paper soaked in head cleaning fluid on the cap station, park the head on it, wait 20–30 minutes.
4. **Clean the cap seal.** Cotton swab and cleaning fluid on the rubber ring and the wiper blade, then dry it. This is the usual culprit behind `done_no_ink`: a ring that no longer seals means the pump is sucking air.
5. **Rest, then charge.** Power off for 8–12 hours, run one power clean (`--level power`), then power off again for 12 hours. Epson's own instruction for power cleaning is the same 12-hour wait; skipping it is how people burn through half a tank for nothing.
6. **Still nothing?** Clogged-solid head or a dead pump assembly. Time for a service centre.

Rules that keep you from making it worse:

- At most 2–3 consecutive cleanings, then a break of several hours. Repeated cleaning without pause is how air gets into the ink path.
- Ink below 1/3 in any tank → do not clean. You are pumping air into the head.
- Never run a power clean on a printer that has just been cleaned three times, and never unplug during one.

---

## About the waste pad counter

Resetting the counter does not empty the waste pad. It tells the printer "the pad is fine again". If the pad really is saturated, ink will eventually leak out of the bottom of the printer and onto whatever it's standing on. Check or replace the pad (or fit an external waste ink bottle) if you have printed a lot. The tool backs up the counter values before writing and reads them back afterwards to confirm the write landed.

---

## Credits and licence

- EEPROM handling: [Epson-Waste-Reset](https://github.com/RxNaison/Epson-Waste-Reset) by RxNaison, Apache-2.0. See [NOTICE](NOTICE).
- This project: MIT, see [LICENSE](LICENSE).
- Not affiliated with Seiko Epson. Writing to the printer's EEPROM is your own risk; the tool only writes the waste pad counter bytes and never touches firmware.

中文说明见 [README.zh-CN.md](README.zh-CN.md).
