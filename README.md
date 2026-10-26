# Lightshow

Fan curve and ambient status light controller for Linux, driving the **Lian Li UNI FAN AL v1.7** USB controller (`0cf2:a101`) and **ASUS Aura** LED controller (`0b05:1939`) with zero external Python dependencies.

## Features

1. **Automatic Fan Speed Control (`--daemon`)**
   * Keeps ASUS motherboard headers (`nct6798` / `nct6775`) in hardware Smart Fan IV auto mode (`pwm_enable = 5`).
   * Scales the Lian Li UNI FAN AL speeds over USB HID based on CPU temperature (`k10temp` `Tctl`):
     * Up to 58°C: 10% (~850 RPM quiet idle)
     * 58°C to 70°C: Ramps smoothly from 10% to 50% (~1350 RPM)
     * 70°C to 85°C: Ramps smoothly from 50% to 100% (1900 RPM)

2. **Priority Status Light Rules (`--sync`)**
   * **Night / Quiet Hours (21:00 to 05:45):** Lights **Off** (`#000000`)
   * **Claude Notification Active:** **Blue** (`#0044ff`)
   * **Critical / Security Updates (or reboot required):** **Red** (`#ff0000`)
   * **Standard System Updates:** **Yellow** (`#ffb400`)
   * **Normal Operations:** **Green** (`#00ff00`)

3. **Web GUI & Crontab Synchronisation (`http://localhost:8484`)**
   * Served by `lianli-lights-web.service` (`systemctl --user`).
   * Displays live active rule, colour, CPU temperature, fan duty cycle, pending APT/mintupdate counts, and active `crontab` entries.
   * Includes colour pickers, quiet-hour schedule editors, and one-click simulation buttons.

4. **Claude Code Hook Integration**
   * `Notification` hook runs `lightshow.py --claude-notify` (turns lights blue when Claude needs input).
   * `UserPromptSubmit` hook runs `lightshow.py --claude-clear` (clears the blue alert as soon as a prompt is submitted).

## Usage

```bash
# Evaluate rules and sync hardware lights immediately
./lightshow.py --sync

# Set a manual colour override (or return to auto mode)
./lightshow.py --color green
./lightshow.py --color off
./lightshow.py --color auto

# Trigger or clear a Claude notification alert
./lightshow.py --claude-notify
./lightshow.py --claude-clear

# Install udev rules and system fan daemon (requires sudo once)
sudo ./lightshow.py --install
```

## Services & Configuration

* **Web UI:** `http://localhost:8484` (`systemctl --user status lianli-lights-web.service`)
* **Fan Daemon:** `systemctl status lianli-al-setup.service`
* **State File:** `~/.config/lianli-lights/state.json`
* **Motherboard Sensor Module:** `/etc/modules-load.d/nct6775.conf`
* **USB Permissions:** `/etc/udev/rules.d/99-lianli-aura.rules`
