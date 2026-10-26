# Lightshow

Fan curve and ambient status light controller for Linux, driving the **Lian Li UNI FAN AL v1.7** USB controller (`0cf2:a101`) and **ASUS Aura** LED controller (`0b05:1939`) with zero external Python dependencies.

## Architecture & How It Runs

The setup is split into three independent pieces so fans and status lights keep working even if the Web GUI is stopped:

1. **System Fan & Hardware Daemon (`lianli-al-setup.service`)**
   * Runs `lightshow.py --daemon` as a system service at boot.
   * Sets ASUS motherboard fan headers (`nct6798` / `nct6775`) to hardware Smart Fan IV auto mode (`pwm_enable = 5`).
   * Polls CPU temperature (`k10temp` `Tctl`) every 3 seconds and scales the Lian Li UNI FAN AL speeds over USB HID:
     * Up to 58°C: **10%** (~850 RPM quiet idle)
     * 58°C to 70°C: Ramps smoothly from **10% to 50%** (~1350 RPM)
     * 70°C to 85°C: Ramps smoothly from **50% to 100%** (1900 RPM)

2. **Scheduled Status Light Sync (`crontab` calling `lightshow.py --sync`)**
   * Runs every 15 minutes (`*/15 * * * *`) and at quiet-hour boundaries (`21:00` and `05:45`).
   * Evaluates status rules in priority order and updates both the Lian Li fan LEDs and ASUS motherboard Aura LEDs:
     1. **Manual Override:** Fixed colour if selected in the Web UI or CLI (`--color <name>`).
     2. **Night / Quiet Hours (21:00 to 05:45):** Lights **Off** (`#000000`)
     3. **Claude Notification Active:** **Blue** (`#0044ff`)
     4. **Critical / Security Updates (or reboot required):** **Red** (`#ff0000`)
     5. **Standard System Updates:** **Yellow** (`#ffb400`)
     6. **Normal Operations:** **Green** (`#00ff00`)

3. **Web GUI (`lianli-lights-web.service` on `http://localhost:8484`)**
   * Runs `lightshow.py --web` as a user systemd service (`systemctl --user`).
   * Displays the active rule, current colour, CPU temperature, fan duty cycle, pending APT/mintupdate counts, and `crontab` entries.
   * Provides colour pickers, quiet-hour schedule editors, and one-click test simulation buttons.
   * **Optional at runtime:** Stopping this service only turns off the `:8484` dashboard; the fan daemon, `crontab` sync, and Claude Code hooks continue working independently.

---

## Quick Health Check & Operations Cheat Sheet

If you come back to this system after a few months, run these commands to check that everything is healthy:

```bash
# 1. Check the system fan curve daemon (should be active/running)
systemctl status lianli-al-setup.service

# 2. Check the user Web GUI service on http://localhost:8484
systemctl --user status lianli-lights-web.service

# 3. Inspect live state, temperature, fan duty cycle, and crontab via HTTP API
curl -s http://localhost:8484/api/status | python3 -m json.tool

# 4. Check installed user cron jobs
crontab -l

# 5. View recent logs or warnings for either service
journalctl -u lianli-al-setup.service -n 50 --no-pager
journalctl --user -u lianli-lights-web.service -n 50 --no-pager
```

### Restarting or Stopping Services

```bash
# Restart the system fan daemon
sudo systemctl restart lianli-al-setup.service

# Restart the Web GUI
systemctl --user restart lianli-lights-web.service

# Stop and disable the Web GUI if you do not want the dashboard running in the background
systemctl --user disable --now lianli-lights-web.service

# Re-enable the Web GUI later
systemctl --user enable --now lianli-lights-web.service
```

---

## CLI Usage

```bash
# Evaluate rules and sync hardware lights immediately
./lightshow.py --sync

# Set a manual colour override (or return to automatic rule mode)
./lightshow.py --color green
./lightshow.py --color off
./lightshow.py --color auto

# Trigger or clear a Claude notification alert
./lightshow.py --claude-notify
./lightshow.py --claude-clear

# Install or refresh the managed crontab block from current state.json settings
./lightshow.py --install-cron

# Install udev rules and the system fan daemon (requires sudo once)
sudo ./lightshow.py --install
```

---

## Full Setup / Rebuild from Scratch

If setting up on a fresh Linux installation:

1. **Enable the motherboard Super I/O sensor module (`nct6775`):**
   ```bash
   echo "nct6775" | sudo tee /etc/modules-load.d/nct6775.conf
   sudo modprobe nct6775
   ```

2. **Install udev permissions and the system fan service:**
   ```bash
   sudo /path/to/lightshow/lightshow.py --install
   ```
   This writes `/etc/udev/rules.d/99-lianli-aura.rules` (granting user access to `/dev/hidraw*` for `0cf2:a101` and `0b05:1939` without `sudo`) and enables `/etc/systemd/system/lianli-al-setup.service`.

3. **Install the user crontab entries:**
   ```bash
   /path/to/lightshow/lightshow.py --install-cron
   ```

4. **Create and enable the user Web GUI service (`~/.config/systemd/user/lianli-lights-web.service`):**
   ```ini
   [Unit]
   Description=Lightshow Fan & Status Light Web GUI (http://localhost:8484)
   After=default.target

   [Service]
   Type=simple
   ExecStart=/usr/bin/python3 %h/code/lightshow/lightshow.py --web
   Restart=always
   RestartSec=3

   [Install]
   WantedBy=default.target
   ```
   Then enable it:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now lianli-lights-web.service
   ```

5. **Configure Claude Code hooks in `~/.claude/settings.json`:**
   ```json
   {
     "hooks": {
       "Notification": [
         {
           "matcher": "",
           "hooks": [
             {
               "type": "command",
               "command": "/path/to/lightshow/lightshow.py --claude-notify"
             }
           ]
         }
       ],
       "UserPromptSubmit": [
         {
           "matcher": "",
           "hooks": [
             {
               "type": "command",
               "command": "/path/to/lightshow/lightshow.py --claude-clear"
             }
           ]
         }
       ]
     }
   }
   ```

---

## Hardware Notes & Protocol Quirks

* **Why Lian Li fans use direct USB speed control instead of motherboard PWM sync:**
  Enabling hardware PWM sync on the Lian Li UNI FAN AL v1.7 controller (`0cf2:a101`) when its 4-pin PWM pass-through cable is unconnected causes the hub to read a floating high PWM pin and lock all fans to 100% (1900 RPM). Instead, `lightshow.py` disables PWM sync per channel (`[0xE0, 0x10, 0x42, 0x10 << channel]`) and sends direct speed bytes (`[0xE0, 0x20 + channel, 0x00, speed_byte]`, where `speed_byte = int((800 + 11 * pct) / 19)`).
* **Lian Li UNI FAN AL colour byte order:**
  The controller expects LED colour payloads in **`R, B, G`** wire order across 32 fan LEDs (`fan_or_edge = 0`) and 48 edge LEDs (`fan_or_edge = 1`) per channel, committed with `[0xE0, 0x10 + fan_or_edge + (channel * 2), 0x01, 0x02, 0x00, brightness_code]` (`0x00` = 100% brightness, `0x08` = 0% off).
* **State & Configuration Paths:**
  * Runtime state & colour settings: `~/.config/lianli-lights/state.json`
  * System fan daemon unit: `/etc/systemd/system/lianli-al-setup.service`
  * User Web GUI unit: `~/.config/systemd/user/lianli-lights-web.service`
  * USB HID udev rules: `/etc/udev/rules.d/99-lianli-aura.rules`
  * Motherboard sensor module config: `/etc/modules-load.d/nct6775.conf`
