#!/usr/bin/env python3
"""
Lightshow: Fan & Status Light Controller (Lian Li UNI FAN AL 0cf2:a101 + ASUS Aura 0b05:1939)
---------------------------------------------------------------------------------------------
Rules (in priority order):
1. Manual Override (if set in Web UI / CLI instead of "auto")
2. Night / Quiet Hours (default 21:00 - 05:45) -> OFF (#000000)
3. Claude Notification active -> BLUE (#0044ff)
4. Critical / Security updates (or reboot required) -> RED (#ff0000)
5. Standard system updates -> YELLOW (#ffb400)
6. Normal operations -> GREEN (#00ff00)

Also runs:
- Web GUI on http://localhost:8484 (--web) with live crontab integration
- Auto fan curve daemon (--daemon) for Lian Li USB fans based on CPU temperature
"""
import datetime
import glob
import http.server
import json
import os
import pwd
import re
import subprocess
import sys
import time


def _resolve_user_home():
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass
    try:
        owner_uid = os.stat(os.path.realpath(__file__)).st_uid
        if owner_uid != 0:
            return pwd.getpwuid(owner_uid).pw_dir
    except Exception:
        pass
    return os.path.expanduser("~")


USER_HOME = _resolve_user_home()
SCRIPT_PATH = os.path.realpath(__file__)
CONFIG_DIR = os.path.join(USER_HOME, ".config", "lianli-lights")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
SYSTEMD_UNIT_PATH = "/etc/systemd/system/lianli-al-setup.service"
USER_WEB_UNIT_PATH = os.path.join(USER_HOME, ".config", "systemd", "user", "lianli-lights-web.service")
UDEV_RULE_PATH = "/etc/udev/rules.d/99-lianli-aura.rules"
WEB_PORT = int(os.environ.get("LIGHTSHOW_PORT", "8484"))

NAMED_COLORS = {
    "off": (0, 0, 0),
    "black": (0, 0, 0),
    "green": (0, 255, 0),
    "yellow": (255, 180, 0),
    "red": (255, 0, 0),
    "blue": (0, 68, 255),
    "white": (153, 153, 153),
    "cyan": (0, 255, 255),
    "purple": (128, 0, 255),
    "orange": (255, 80, 0),
}

DEFAULT_STATE = {
    "mode": "auto",  # "auto" or a manual colour name/hex
    "quiet_enabled": True,
    "quiet_start": "21:00",
    "quiet_end": "05:45",
    "cron_interval_mins": 15,
    "colors": {
        "normal": "#00ff00",
        "updates": "#ffb400",
        "critical": "#ff0000",
        "claude": "#0044ff",
        "quiet": "#000000",
    },
    "claude_notification": False,
    "claude_notified_at": None,
    "claude_message": "",
    "claude_timeout_mins": 30,
    "simulated_state": None,  # None, "normal", "updates", "critical", "claude", "off"
    "updates_cache": {
        "checked_at": 0,
        "regular_count": 0,
        "security_count": 0,
        "reboot_required": False,
        "packages": [],
        "security_packages": [],
    },
    "last_applied": {
        "hex": "#00ff00",
        "reason": "Normal operations",
        "updated_at": "",
    },
}


def parse_color(color_str):
    s = str(color_str).strip().lower().lstrip("#")
    if s in NAMED_COLORS:
        return NAMED_COLORS[s]
    if len(s) == 6:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    raise ValueError(f"Unknown colour '{color_str}'")


def rgb_to_hex(rgb):
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def load_state():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    state = json.loads(json.dumps(DEFAULT_STATE))
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for k, v in loaded.items():
                if isinstance(v, dict) and k in state and isinstance(state[k], dict):
                    state[k].update(v)
                else:
                    state[k] = v
        except Exception:
            pass
    return state


def save_state(state):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)
    try:
        os.chmod(STATE_FILE, 0o666)
    except Exception:
        pass


def is_in_quiet_hours(now_dt, start_str, end_str):
    try:
        sh, sm = [int(x) for x in start_str.split(":")]
        eh, em = [int(x) for x in end_str.split(":")]
    except Exception:
        sh, sm, eh, em = 21, 0, 5, 45
    now_mins = now_dt.hour * 60 + now_dt.minute
    start_mins = sh * 60 + sm
    end_mins = eh * 60 + em
    if start_mins <= end_mins:
        return start_mins <= now_mins < end_mins
    return now_mins >= start_mins or now_mins < end_mins


def check_system_updates(state, force=False):
    cache = state.get("updates_cache", {})
    now = time.time()
    if not force and cache.get("checked_at", 0) and (now - cache["checked_at"] < 600):
        return cache

    regular_pkgs = []
    security_pkgs = []
    try:
        res = subprocess.run(
            ["apt-get", "-s", "dist-upgrade"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        for line in res.stdout.splitlines():
            if line.startswith("Inst "):
                parts = line.split()
                pkg = parts[1] if len(parts) > 1 else line
                regular_pkgs.append(pkg)
                if "-security" in line.lower() or "security" in line.lower():
                    security_pkgs.append(pkg)
    except Exception:
        pass

    if os.path.exists("/usr/bin/mintupdate-cli"):
        try:
            res_sec = subprocess.run(
                ["/usr/bin/mintupdate-cli", "-s", "list"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            for line in res_sec.stdout.splitlines():
                line = line.strip()
                if line and not line.startswith("Reading") and not line.startswith("Building"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] not in security_pkgs:
                        security_pkgs.append(parts[1])
        except Exception:
            pass

    reboot_req = os.path.exists("/var/run/reboot-required")
    cache = {
        "checked_at": now,
        "regular_count": len(regular_pkgs),
        "security_count": len(security_pkgs),
        "reboot_required": reboot_req,
        "packages": regular_pkgs[:25],
        "security_packages": security_pkgs[:25],
    }
    state["updates_cache"] = cache
    return cache


def evaluate_desired_color(state, force_update_check=False):
    """Return (rgb_tuple, hex_str, reason_str) based on rules."""
    now_dt = datetime.datetime.now()
    colors = state.get("colors", DEFAULT_STATE["colors"])

    if state.get("claude_notification") and state.get("claude_notified_at"):
        timeout_mins = int(state.get("claude_timeout_mins", 30) or 30)
        if timeout_mins > 0 and (time.time() - state["claude_notified_at"] > timeout_mins * 60):
            state["claude_notification"] = False
            state["claude_message"] = ""

    sim = state.get("simulated_state")
    if sim:
        if sim == "off":
            rgb = parse_color(colors.get("quiet", "#000000"))
            return rgb, rgb_to_hex(rgb), "Simulated: Night / Off"
        if sim == "claude":
            rgb = parse_color(colors.get("claude", "#0044ff"))
            return rgb, rgb_to_hex(rgb), "Simulated: Claude Notification (Blue)"
        if sim == "critical":
            rgb = parse_color(colors.get("critical", "#ff0000"))
            return rgb, rgb_to_hex(rgb), "Simulated: Critical Updates (Red)"
        if sim == "updates":
            rgb = parse_color(colors.get("updates", "#ffb400"))
            return rgb, rgb_to_hex(rgb), "Simulated: System Updates (Yellow)"
        if sim == "normal":
            rgb = parse_color(colors.get("normal", "#00ff00"))
            return rgb, rgb_to_hex(rgb), "Simulated: Normal Operations (Green)"

    mode = state.get("mode", "auto")
    if mode != "auto":
        rgb = parse_color(mode)
        return rgb, rgb_to_hex(rgb), f"Manual override ({mode})"

    if state.get("quiet_enabled", True) and is_in_quiet_hours(
        now_dt, state.get("quiet_start", "21:00"), state.get("quiet_end", "05:45")
    ):
        rgb = parse_color(colors.get("quiet", "#000000"))
        return (
            rgb,
            rgb_to_hex(rgb),
            f"Quiet hours ({state.get('quiet_start', '21:00')} to {state.get('quiet_end', '05:45')})",
        )

    if state.get("claude_notification"):
        rgb = parse_color(colors.get("claude", "#0044ff"))
        msg = state.get("claude_message") or "Claude waiting for attention"
        return rgb, rgb_to_hex(rgb), f"Claude Notification: {msg}"

    upd = check_system_updates(state, force=force_update_check)
    if upd.get("security_count", 0) > 0 or upd.get("reboot_required"):
        rgb = parse_color(colors.get("critical", "#ff0000"))
        sec_n = upd.get("security_count", 0)
        extra = " + reboot required" if upd.get("reboot_required") else ""
        return rgb, rgb_to_hex(rgb), f"CRITICAL updates ({sec_n} security pkg(s){extra})"

    if upd.get("regular_count", 0) > 0:
        rgb = parse_color(colors.get("updates", "#ffb400"))
        return rgb, rgb_to_hex(rgb), f"System updates available ({upd['regular_count']} pkg(s))"

    rgb = parse_color(colors.get("normal", "#00ff00"))
    return rgb, rgb_to_hex(rgb), "Normal operations (all clear)"


def find_lianli_hidraw():
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        uevent = os.path.join(path, "device/uevent")
        if os.path.exists(uevent):
            with open(uevent, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().upper()
                if "00000CF2:0000A101" in content:
                    return "/dev/" + os.path.basename(path)
    return None


def find_cpu_temp_file():
    for path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name_file = os.path.join(path, "name")
        if os.path.exists(name_file):
            with open(name_file, "r", encoding="utf-8", errors="ignore") as f:
                if f.read().strip() == "k10temp":
                    t1 = os.path.join(path, "temp1_input")
                    if os.path.exists(t1):
                        return t1
    return None


def enable_motherboard_hw_auto_fans():
    for path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name_file = os.path.join(path, "name")
        if os.path.exists(name_file):
            with open(name_file, "r", encoding="utf-8", errors="ignore") as f:
                if f.read().strip() in ("nct6798", "nct6775"):
                    for pwm_enable in sorted(glob.glob(os.path.join(path, "pwm[1-6]_enable"))):
                        try:
                            with open(pwm_enable, "w", encoding="utf-8") as pf:
                                pf.write("5\n")
                        except Exception:
                            pass


def set_asus_aura_color(rgb):
    r, g, b = rgb
    if os.path.exists("/usr/bin/liquidctl"):
        if (r, g, b) == (0, 0, 0):
            cmd = ["/usr/bin/liquidctl", "--match", "Aura", "set", "sync", "color", "off"]
        else:
            cmd = ["/usr/bin/liquidctl", "--match", "Aura", "set", "sync", "color", "fixed", f"{r:02x}{g:02x}{b:02x}"]
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def read_cpu_temp_c(temp_file=None):
    if temp_file is None:
        temp_file = find_cpu_temp_file()
    if not temp_file or not os.path.exists(temp_file):
        return 45.0
    try:
        with open(temp_file, "r", encoding="utf-8") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return 45.0


def calc_speed_pct(temp_c):
    if temp_c <= 58.0:
        return 10.0
    if temp_c <= 70.0:
        return 10.0 + (temp_c - 58.0) * (40.0 / 12.0)
    if temp_c <= 85.0:
        return 50.0 + (temp_c - 70.0) * (50.0 / 15.0)
    return 100.0


def pad(data, length):
    return bytes(data) + bytes(max(0, length - len(data)))


def set_lianli_speed(fd, speed_pct):
    speed_pct = max(0.0, min(100.0, float(speed_pct)))
    speed_byte = int((800.0 + (11.0 * speed_pct)) / 19.0)
    for x in range(4):
        os.write(fd, pad([0xE0, 0x20 + x, 0x00, speed_byte], 65))
        time.sleep(0.05)


def set_lianli_leds(fd, rgb):
    r, g, b = rgb
    is_off = (r, g, b) == (0, 0, 0)
    brightness_code = 0x08 if is_off else 0x00

    os.write(fd, pad([0xE0, 0x10, 0x41, 0x00, 0x00, 0x00, 0x00], 65))
    time.sleep(0.05)

    for channel in range(4):
        for fan_or_edge, num_leds in ((0, 32), (1, 48)):
            os.write(fd, pad([0xE0, 0x10, 0x40, channel + 1, 4], 65))
            time.sleep(0.01)

            color_payload = [r, b, g] * num_leds
            os.write(fd, pad([0xE0, 0x30 + fan_or_edge + (channel * 2)] + color_payload, 146))
            time.sleep(0.01)

            os.write(
                fd,
                pad(
                    [
                        0xE0,
                        0x10 + fan_or_edge + (channel * 2),
                        0x01,
                        0x02,
                        0x00,
                        brightness_code,
                    ],
                    65,
                ),
            )
            time.sleep(0.01)


def init_lianli_al(rgb=(0, 0, 0), speed_pct=None):
    dev_path = find_lianli_hidraw()
    if not dev_path:
        return False

    fd = os.open(dev_path, os.O_RDWR)
    try:
        set_lianli_leds(fd, rgb)
        if speed_pct is not None:
            for x in range(4):
                channel_byte = 0x10 << x
                os.write(fd, pad([0xE0, 0x10, 0x42, channel_byte], 65))
                time.sleep(0.05)
            set_lianli_speed(fd, speed_pct)
        return True
    finally:
        os.close(fd)


def sync_lights(force_update_check=False, force_hardware=False):
    state = load_state()
    rgb, hex_color, reason = evaluate_desired_color(state, force_update_check=force_update_check)
    prev_hex = state.get("last_applied", {}).get("hex")

    if force_hardware or prev_hex != hex_color:
        set_asus_aura_color(rgb)
        init_lianli_al(rgb=rgb, speed_pct=None)

    state["last_applied"] = {
        "hex": hex_color,
        "reason": reason,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_state(state)
    return state


def build_cron_block(state):
    sh, sm = [int(x) for x in state.get("quiet_start", "21:00").split(":")]
    eh, em = [int(x) for x in state.get("quiet_end", "05:45").split(":")]
    interval = max(1, min(60, int(state.get("cron_interval_mins", 15) or 15)))
    lines = [
        "# BEGIN LIANLI_LIGHTS",
        f"# Auto-generated by Lightshow Controller (http://localhost:{WEB_PORT})",
        f"{sm} {sh} * * * {SCRIPT_PATH} --sync >/dev/null 2>&1",
        f"{em} {eh} * * * {SCRIPT_PATH} --sync >/dev/null 2>&1",
        f"*/{interval} * * * * {SCRIPT_PATH} --sync >/dev/null 2>&1",
        "# END LIANLI_LIGHTS",
    ]
    return "\n".join(lines)


def get_current_crontab():
    res = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if res.returncode != 0:
        return ""
    return res.stdout


def update_user_crontab(state):
    existing = get_current_crontab()
    block = build_cron_block(state)
    pattern = re.compile(r"# BEGIN LIANLI_LIGHTS.*?# END LIANLI_LIGHTS", re.DOTALL)
    if pattern.search(existing):
        new_cron = pattern.sub(block, existing)
    else:
        new_cron = (existing.rstrip() + "\n\n" + block + "\n").lstrip()
    subprocess.run(["crontab", "-"], input=new_cron, text=True, check=True)
    return new_cron


def run_daemon():
    enable_motherboard_hw_auto_fans()
    temp_file = find_cpu_temp_file()
    state = sync_lights(force_update_check=True, force_hardware=True)
    rgb = parse_color(state["last_applied"]["hex"])
    init_lianli_al(rgb=rgb, speed_pct=calc_speed_pct(read_cpu_temp_c(temp_file)))
    last_speed = None
    last_sync_check = time.time()

    while True:
        try:
            temp_c = read_cpu_temp_c(temp_file)
            target_speed = round(calc_speed_pct(temp_c))
            if last_speed is None or abs(target_speed - last_speed) >= 3:
                dev_path = find_lianli_hidraw()
                if dev_path:
                    fd = os.open(dev_path, os.O_RDWR)
                    try:
                        set_lianli_speed(fd, target_speed)
                        last_speed = target_speed
                    finally:
                        os.close(fd)
            if time.time() - last_sync_check >= 30:
                sync_lights(force_update_check=False, force_hardware=False)
                last_sync_check = time.time()
        except Exception as e:
            print(f"Warning in fan loop: {e}", file=sys.stderr)
        time.sleep(3)


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lightshow: Fan & Light Control</title>
<style>
  :root {
    --bg: #0f1218;
    --panel: #171c26;
    --border: #283040;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #3b82f6;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 28px 20px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    max-width: 920px;
    margin-inline: auto;
  }
  h1 { margin: 0 0 6px; font-size: 1.5rem; display: flex; align-items: center; gap: 12px; }
  .sub { color: var(--muted); font-size: 0.92rem; margin-bottom: 22px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 16px; }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px;
  }
  .card h2 { margin: 0 0 12px; font-size: 1.05rem; color: #c9d1d9; }
  .status-orb {
    width: 22px; height: 22px; border-radius: 50%;
    display: inline-block;
    border: 2px solid rgba(255,255,255,0.25);
    box-shadow: 0 0 14px currentColor;
  }
  .stat-row { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 0.92rem; }
  .stat-row:last-child { border-bottom: none; }
  .btn-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
  button {
    background: #212836;
    color: var(--text);
    border: 1px solid var(--border);
    padding: 8px 13px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.88rem;
    font-weight: 500;
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }
  button:hover { background: #2c3548; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.active { outline: 2px solid var(--accent); }
  .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  label { display: flex; align-items: center; justify-content: space-between; margin: 9px 0; font-size: 0.92rem; }
  input[type="time"], input[type="number"], select {
    background: #0f1218; color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 5px 8px; font-size: 0.9rem;
  }
  input[type="color"] {
    background: none; border: 1px solid var(--border); border-radius: 6px; width: 42px; height: 30px; cursor: pointer;
  }
  pre {
    background: #0a0d12; border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; font-size: 0.82rem; overflow-x: auto; color: #a5d6ff; margin: 8px 0 0;
  }
  .toast {
    position: fixed; bottom: 20px; right: 20px; background: #1f6feb; color: #fff;
    padding: 10px 16px; border-radius: 8px; display: none; font-size: 0.9rem;
  }
</style>
</head>
<body>
  <h1>
    <span id="orb" class="status-orb"></span>
    Lightshow: Fan & Light Controller
  </h1>
  <div class="sub">Lian Li UNI FAN AL v1.7 (0cf2:a101) + ASUS Aura &bull; Auto Fan Curve + Crontab Rules</div>

  <div class="grid">
    <div class="card">
      <h2>Live Status</h2>
      <div class="stat-row"><span>Active Rule</span><strong id="st-reason">-</strong></div>
      <div class="stat-row"><span>Active Colour</span><code id="st-hex">-</code></div>
      <div class="stat-row"><span>CPU Temp / Lian Li Fan Duty</span><strong id="st-temp">-</strong></div>
      <div class="stat-row"><span>Claude Notification</span><strong id="st-claude">-</strong></div>
      <div class="stat-row"><span>Pending System Updates</span><strong id="st-updates">-</strong></div>
      <div class="stat-row"><span>Critical / Security Updates</span><strong id="st-security">-</strong></div>
      <div class="btn-row" style="margin-top:14px">
        <button onclick="apiPost('/api/claude-clear')">Dismiss Claude Alert</button>
        <button onclick="apiPost('/api/refresh-updates')">Re-check APT Updates</button>
      </div>
    </div>

    <div class="card">
      <h2>Rule Colours & Schedule</h2>
      <label><span>Normal Operations</span><input type="color" id="col-normal"></label>
      <label><span>System Updates Available</span><input type="color" id="col-updates"></label>
      <label><span>CRITICAL / Security Updates</span><input type="color" id="col-critical"></label>
      <label><span>Claude Notification</span><input type="color" id="col-claude"></label>
      <hr style="border:0;border-top:1px solid var(--border);margin:12px 0">
      <label>
        <span>Quiet Hours (Lights Off)</span>
        <input type="checkbox" id="quiet-enabled">
      </label>
      <label><span>Lights Off At (Night)</span><input type="time" id="quiet-start"></label>
      <label><span>Lights On At (Morning)</span><input type="time" id="quiet-end"></label>
      <label><span>Cron Poll Interval (mins)</span><input type="number" id="cron-interval" min="1" max="60" style="width:70px"></label>
      <div class="btn-row">
        <button class="primary" onclick="saveSettings()">Save & Sync Crontab</button>
      </div>
    </div>
  </div>

  <div class="card" style="margin-bottom:16px">
    <h2>Test / Simulate States (or Force Override)</h2>
    <div class="btn-row">
      <button id="btn-auto" onclick="setSim(null)">Auto (Follow Rules)</button>
      <button id="btn-normal" onclick="setSim('normal')"><span class="dot" style="background:#00ff00"></span>Normal (Green)</button>
      <button id="btn-updates" onclick="setSim('updates')"><span class="dot" style="background:#ffb400"></span>Updates (Yellow)</button>
      <button id="btn-critical" onclick="setSim('critical')"><span class="dot" style="background:#ff0000"></span>Critical (Red)</button>
      <button id="btn-claude" onclick="setSim('claude')"><span class="dot" style="background:#0044ff"></span>Claude Alert (Blue)</button>
      <button id="btn-off" onclick="setSim('off')"><span class="dot" style="background:#333;border:1px solid #666"></span>Lights Off</button>
    </div>
  </div>

  <div class="card">
    <h2>Active User Crontab (<code>crontab -l</code>)</h2>
    <pre id="crontab-view">Loading...</pre>
  </div>

  <div id="toast" class="toast">Saved!</div>

<script>
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2200);
}

async function loadStatus() {
  const r = await fetch('/api/status');
  const d = await r.json();
  const s = d.state;
  const hex = s.last_applied?.hex || '#00ff00';
  const orb = document.getElementById('orb');
  orb.style.background = hex;
  orb.style.color = hex === '#000000' ? 'transparent' : hex;

  document.getElementById('st-reason').textContent = s.last_applied?.reason || '-';
  document.getElementById('st-hex').textContent = hex;
  document.getElementById('st-temp').textContent = `${d.cpu_temp_c.toFixed(1)} °C (${d.fan_speed_pct.toFixed(0)}%)`;
  document.getElementById('st-claude').textContent = s.claude_notification ? `ACTIVE (${s.claude_message || 'Attention needed'})` : 'None';
  document.getElementById('st-updates').textContent = `${s.updates_cache?.regular_count || 0} package(s)`;
  document.getElementById('st-security').textContent = `${s.updates_cache?.security_count || 0} security` + (s.updates_cache?.reboot_required ? ' (Reboot required)' : '');

  document.getElementById('col-normal').value = s.colors.normal;
  document.getElementById('col-updates').value = s.colors.updates;
  document.getElementById('col-critical').value = s.colors.critical;
  document.getElementById('col-claude').value = s.colors.claude;
  document.getElementById('quiet-enabled').checked = s.quiet_enabled;
  document.getElementById('quiet-start').value = s.quiet_start;
  document.getElementById('quiet-end').value = s.quiet_end;
  document.getElementById('cron-interval').value = s.cron_interval_mins || 15;
  document.getElementById('crontab-view').textContent = d.crontab || '(empty)';

  ['auto','normal','updates','critical','claude','off'].forEach(k => {
    const el = document.getElementById('btn-' + k);
    if (el) el.classList.toggle('active', (s.simulated_state || 'auto') === k);
  });
}

async function apiPost(path, body={}) {
  await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  await loadStatus();
  showToast('Updated!');
}

async function setSim(sim) {
  await apiPost('/api/simulate', {simulated_state: sim});
}

async function saveSettings() {
  await apiPost('/api/settings', {
    quiet_enabled: document.getElementById('quiet-enabled').checked,
    quiet_start: document.getElementById('quiet-start').value,
    quiet_end: document.getElementById('quiet-end').value,
    cron_interval_mins: parseInt(document.getElementById('cron-interval').value, 10) || 15,
    colors: {
      normal: document.getElementById('col-normal').value,
      updates: document.getElementById('col-updates').value,
      critical: document.getElementById('col-critical').value,
      claude: document.getElementById('col-claude').value,
      quiet: '#000000'
    }
  });
}

loadStatus();
setInterval(loadStatus, 5000);
</script>
</body>
</html>
"""


class WebHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/status"):
            state = sync_lights(force_update_check=False, force_hardware=False)
            temp_c = read_cpu_temp_c()
            self._send_json(
                {
                    "state": state,
                    "cpu_temp_c": temp_c,
                    "fan_speed_pct": calc_speed_pct(temp_c),
                    "crontab": get_current_crontab(),
                }
            )
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        data = json.loads(raw) if raw else {}
        state = load_state()

        if self.path == "/api/simulate":
            state["simulated_state"] = data.get("simulated_state")
            if state["simulated_state"] is None:
                state["mode"] = "auto"
            save_state(state)
            state = sync_lights(force_update_check=False, force_hardware=True)
            self._send_json({"ok": True, "state": state})
        elif self.path == "/api/claude-clear":
            state["claude_notification"] = False
            state["claude_message"] = ""
            state["simulated_state"] = None
            save_state(state)
            state = sync_lights(force_update_check=False, force_hardware=True)
            self._send_json({"ok": True, "state": state})
        elif self.path == "/api/claude-notify":
            state["claude_notification"] = True
            state["claude_notified_at"] = time.time()
            state["claude_message"] = data.get("message", "Claude waiting for input")
            save_state(state)
            state = sync_lights(force_update_check=False, force_hardware=True)
            self._send_json({"ok": True, "state": state})
        elif self.path == "/api/refresh-updates":
            check_system_updates(state, force=True)
            save_state(state)
            state = sync_lights(force_update_check=False, force_hardware=True)
            self._send_json({"ok": True, "state": state})
        elif self.path == "/api/settings":
            for k in ("quiet_enabled", "quiet_start", "quiet_end", "cron_interval_mins"):
                if k in data:
                    state[k] = data[k]
            if "colors" in data and isinstance(data["colors"], dict):
                state["colors"].update(data["colors"])
            save_state(state)
            update_user_crontab(state)
            state = sync_lights(force_update_check=False, force_hardware=True)
            self._send_json({"ok": True, "state": state})
        else:
            self.send_error(404)


def run_web():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", WEB_PORT), WebHandler)
    print(f"Lightshow Web UI listening on http://localhost:{WEB_PORT}")
    server.serve_forever()


def install_udev_rules():
    rules = """# Allow user access to Lian Li UNI FAN AL (0cf2:a101) and ASUS Aura LED Controller (0b05:1939)
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0cf2", ATTRS{idProduct}=="a101", MODE="0666", TAG+="uaccess"
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0b05", ATTRS{idProduct}=="1939", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTR{idVendor}=="0cf2", ATTR{idProduct}=="a101", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTR{idVendor}=="0b05", ATTR{idProduct}=="1939", MODE="0666", TAG+="uaccess"
"""
    with open(UDEV_RULE_PATH, "w", encoding="utf-8") as f:
        f.write(rules)
    subprocess.run(["udevadm", "control", "--reload-rules"], check=True)
    subprocess.run(["udevadm", "trigger"], check=True)


def install_systemd_service():
    unit = f"""[Unit]
Description=Lian Li UNI FAN AL Auto Fan & LED Daemon
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 {SCRIPT_PATH} --daemon
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    with open(SYSTEMD_UNIT_PATH, "w", encoding="utf-8") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "lianli-al-setup.service"], check=True)
    subprocess.run(["systemctl", "restart", "lianli-al-setup.service"], check=True)


if __name__ == "__main__":
    if "--color" in sys.argv:
        idx = sys.argv.index("--color")
        val = sys.argv[idx + 1].strip().lower()
        state = load_state()
        if val == "auto":
            state["mode"] = "auto"
            state["simulated_state"] = None
            save_state(state)
            sync_lights(force_update_check=False, force_hardware=True)
            print("Switched lights to AUTO rule mode.")
        else:
            chosen_rgb = parse_color(val)
            state["mode"] = rgb_to_hex(chosen_rgb)
            state["simulated_state"] = None
            save_state(state)
            sync_lights(force_update_check=False, force_hardware=True)
            print(f"Set manual light colour to {val} ({rgb_to_hex(chosen_rgb)}).")
    elif "--claude-notify" in sys.argv:
        msg = "Claude waiting for input"
        try:
            if not sys.stdin.isatty():
                raw_in = sys.stdin.read().strip()
                if raw_in:
                    payload = json.loads(raw_in)
                    msg = payload.get("message") or payload.get("title") or msg
        except Exception:
            pass
        state = load_state()
        state["claude_notification"] = True
        state["claude_notified_at"] = time.time()
        state["claude_message"] = msg
        save_state(state)
        sync_lights(force_update_check=False, force_hardware=True)
    elif "--claude-clear" in sys.argv:
        state = load_state()
        if state.get("claude_notification"):
            state["claude_notification"] = False
            state["claude_message"] = ""
            save_state(state)
            sync_lights(force_update_check=False, force_hardware=True)
    elif "--sync" in sys.argv:
        sync_lights(force_update_check=True, force_hardware=True)
    elif "--web" in sys.argv:
        run_web()
    elif "--daemon" in sys.argv:
        run_daemon()
    elif "--install-cron" in sys.argv:
        update_user_crontab(load_state())
        print("Installed crontab entries.")
    elif "--install" in sys.argv:
        if os.geteuid() != 0:
            print(f"Please run --install with sudo: sudo {SCRIPT_PATH} --install")
            sys.exit(1)
        install_udev_rules()
        install_systemd_service()
    else:
        sync_lights(force_update_check=False, force_hardware=True)
