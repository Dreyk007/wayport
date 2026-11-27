# wayport

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Platform: Linux | KVM](https://img.shields.io/badge/Platform-Linux%20%7C%20KVM-orange.svg)](#)
[![Desktop: KDE Plasma 6 Wayland](https://img.shields.io/badge/Desktop-KDE%20Plasma%206%20(Wayland)-informational.svg)](#)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](#)

**Seamless KVM Guest & Remote Linux Application Integration for KDE Plasma 6 (Wayland) over `vhost-vsock`, Pure SSH, or Hybrid Transport.**

`wayport` brings `distrobox-export` and Qubes-like seamless window integration to KVM virtual machines and remote Linux hosts. It exports GUI applications directly into your host KDE Plasma 6 launcher (**KRunner** / **Kickoff**), rendering each remote window as an independent native Wayland surface on the host with full **System Tray (`StatusNotifierItem` + `DbusMenu`)**, **Desktop Notifications**, **KWallet 6**, and **KDE File Dialog (`KFileWidget`)** integration.

> For a deep dive into the wire protocols, Wayland stream masking, portal routing, and security model, see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Why `wayport`?

| Solution | Multi-Monitor Fractional Scaling | Chromium / Electron Quirks | System Tray & Notifications | Transport Flexibility |
| :--- | :--- | :--- | :--- | :--- |
| **SPICE (`virt-manager`)** | Broken (single framebuffer DPI, blurry scaling) | N/A (full desktop) | Trapped inside VM | Local KVM only |
| **Xpra / FreeRDP** | Blurry or laggy geometry recalculation on mixed-DPI | Often breaks CSD / Ozone | Partial | High overhead (video encoding) |
| **wprs** | Partial protocol coverage | Breaks across distro Rust/rkyv versions | None | Medium (CPU compression) |
| **Raw `waypipe ssh`** | Native (via Host KWin) | Loses KWallet, opens file dialogs in VM, blocks host sleep | None | SSH only, stalls on `ControlMaster` & `dmabuf` |
| **`wayport`** | **100% Native** (per-window `wp_fractional_scale_v1`) | **Full support** (Ozone Wayland, KWallet 6, native KDE file dialogs, safe singleton takeover) | **Full Bridge** (`StatusNotifierItem`, `DbusMenu`, `libnotify`) | **3 Modes:** `hybrid`, `vsock` (zero SSH), `ssh` (zero KVM/VSOCK) |

---

## Transport Modes

Choose the transport mode that fits your setup via `--mode` (`-m`), `WAYPORT_MODE`, or `DEFAULT_TRANSPORT_MODE` at the top of [`wayport`](wayport):

| Mode | Control Plane | Data Plane (Wayland + Tray) | Typical Control Latency | Best For |
| :--- | :--- | :--- | :--- | :--- |
| **`hybrid`** *(default)* | SSH (`BatchMode=yes`) | Native kernel `AF_VSOCK` (`vhost-vsock`) | ~0.4 s | Everyday KVM VMs with SSH: zero guest daemon setup, automatic script sync, and zero TCP/IP graphics overhead |
| **`vsock`** *(alias: `socket`)* | `AF_VSOCK` JSON-RPC (`:5000`) | Native kernel `AF_VSOCK` | **~0.1 s (4× faster)** | Air-gapped / networkless KVM VMs without `sshd` or virtual NIC, and lowest possible launch latency |
| **`ssh`** | SSH (`BatchMode=yes`) | `ssh -R` Unix-socket tunnel + SSH `stdio` | ~0.4 s | Remote Linux PCs, workstations, servers over LAN / WireGuard / Tailscale / WAN, containers, or VMs without Virtio VSOCK |

### Choosing Between `hybrid`, `vsock`, and `ssh`

* **Why both `hybrid` and `vsock` exist:** In both modes, **100% of graphics (Wayland) and System Tray / Notifications flow over kernel `AF_VSOCK`** — even on older LTS hosts (such as Ubuntu 24.04 with C `waypipe 0.8.x`, which lacks a native `--vsock` flag), because `wayport` opens the host `AF_VSOCK` socket directly in Python and bridges it to the host `waypipe client` in memory.
  * Choose **`hybrid`** when you want **zero guest daemon setup** — just configure SSH keys (`ssh-copy-id`) and `wayport` automatically deploys/updates guest helpers and works with dynamic VSOCK CIDs (`<cid auto="yes"/>`).
  * Choose **`vsock`** when you want **zero SSH dependency at runtime** (even removing `sshd` or the VM's NIC completely) and **4× faster command latency** — requires running `wayport install-guest` once and pinning a fixed CID (`<cid auto="no" address="3"/>`) in libvirt XML.
* Choose **`ssh`** whenever Virtio VSOCK is unavailable — e.g., **physical remote Linux PCs, workstations over LAN/VPN/WAN, cloud VMs, or containers**. Uses `lz4` compression by default (or `--compression zstd` on slow WAN links).

---

## Key Features

* **Three First-Class Transport Modes (`hybrid`, `vsock`/`socket`, `ssh`):** Seamlessly switch between kernel `AF_VSOCK`, pure SSH tunneling, or hybrid mode without changing your workflow.
* **True Per-Monitor Fractional Scaling:** Each application window is forwarded as an independent `wl_surface` to the host KWin compositor for crisp scaling across mixed-DPI monitors.
* **Cross-Version `waypipe` Bridge:** Transparently bridges older host `waypipe` versions (e.g., C `waypipe 0.8.x` on Ubuntu 24.04) with newer guest versions (e.g., Rust `waypipe 0.10+/0.11+` on Debian Testing/Arch), including pure `vsock` mode.
* **Automatic Electron / Chromium Support:** Auto-detects Electron/Chromium apps inside the guest (`resources/app.asar`, `icudtl.dat`, `chrome_crashpad_handler`) to inject `--ozone-platform=wayland` and `--password-store=kwallet6`, and includes a built-in `wl_shm` `ready-to-show` workaround (`ELECTRON_SHM_NUDGE_APPS`) so newer Electron apps like Qoder render at full native frame rates without slow DMABUF readbacks.
* **System Tray & Desktop Notification Bridge (`StatusNotifierItem` + `DbusMenu`):**
  * Mirrors guest tray icons (Chrome, Claude Desktop, Antigravity, VS Code, Telegram, etc.) and context menus into the host KDE Plasma 6 panel.
  * Includes an embedded **`org.kde.StatusNotifierWatcher`** for **100% headless VMs (`sddm` disabled)**, plus fast `<0.1ms` **`org.freedesktop.Notifications`** and **`org.freedesktop.ScreenSaver`** responders.
* **Host Sleep Protection:** Masks `zwp_idle_inhibit_manager_v1` across chunk boundaries on uncompressed Wayland streams so background Electron apps never block laptop sleep.
* **Native KDE Plasma 6 File Dialogs on the Host:** Dynamically routes `plasma-xdg-desktop-portal-kde.service` (using fast non-blocking restarts) to the active `waypipe` socket so Open/Save dialogs appear on the host monitor.
* **Per-VM Isolation & Deterministic Teardown:** Per-VM `systemd --user` tray services (`wayport-tray-<vm>.service`) with automatic port-conflict protection, full process-group teardown for host `waypipe client` workers, `WAYPORT_INSTANCE_ID` process tracking, and menu-gated tray-quit cleanup.
* **Single-File Top-of-File Configuration:** All user defaults live in one documented block at the top of [`wayport`](wayport) and propagate automatically to guest scripts via content hashing.

---

## Prerequisites

### 1. Install Packages

**On the Host (Ubuntu / TuxedoOS / Debian / Arch / Fedora equivalents):**
```bash
sudo apt update && sudo apt install -y waypipe python3-dbus python3-gi gir1.2-gdkpixbuf-2.0
```

**Inside the Guest VM / Remote Linux Host:**
```bash
sudo apt update && sudo apt install -y waypipe python3-dbus python3-gi gir1.2-gdkpixbuf-2.0 \
    xdg-desktop-portal xdg-desktop-portal-kde kwallet6 dbus-x11
```

### 2. Mode-Specific Setup

#### For `hybrid` and `vsock` modes (KVM `vhost-vsock` + VirGL)
1. **Load `vhost_vsock` on the host:**
   ```bash
   sudo modprobe vhost_vsock
   echo "vhost_vsock" | sudo tee /etc/modules-load.d/vhost-vsock.conf
   ```
2. **Attach Virtio VSOCK (and optional Virtio 3D) in `virt-manager` XML (`<devices>`):**
   In `virt-manager`, enable XML editing (`Edit → Preferences → Enable XML editing`), shut down the VM, open its **Overview → XML** tab (or run `virsh edit <vm-name>`), and add inside `<devices>`:
   ```xml
   <video>
     <model type="virtio" heads="1" primary="yes">
       <acceleration accel3d="yes"/>
     </model>
   </video>
   <vsock model="virtio">
     <cid auto="no" address="3"/>
   </vsock>
   ```
   > **About `<cid>`:** A VSOCK **CID (Context ID)** is the VM's numeric address on `AF_VSOCK` (`0` = hypervisor, `1` = local loopback, `2` = host, `3+` = guest VMs). With `auto="yes"`, libvirt assigns the next free number on each boot (which works fine for `hybrid` mode, since the host listens on `CID_ANY`). For pure **`vsock` mode**, pinning `<cid auto="no" address="3"/>` (and `4`, `5`, … for additional VMs) guarantees that exported `.desktop` launchers with `--cid 3` always reach the exact same VM across reboots.
3. **Headless GPU permissions inside the VM (optional if disabling `sddm`):**
   ```bash
   sudo usermod -aG render,video "$USER" && sudo loginctl enable-linger "$USER"
   # Optional: disable guest display manager to save RAM
   sudo systemctl disable --now sddm
   ```

#### For `hybrid` and `ssh` modes (SSH access)
1. Configure passwordless SSH key authentication:
   ```bash
   ssh-copy-id user@linux-vm.local
   ```
2. For **`ssh` mode**, ensure the remote `sshd_config` allows Unix socket forwarding (enabled by default on Debian/Ubuntu):
   ```text
   AllowStreamLocalForwarding yes
   ```

#### For `vsock` (pure socket, zero-SSH) mode
Install and enable the guest control daemon (`wayport-guest.service`, listening on `AF_VSOCK` port `5000` for Host CID `2` only):
* **One-time bootstrap over SSH from the host** (after which `sshd` can even be stopped/removed):
  ```bash
  wayport -t user@linux-vm.local install-guest
  ```
* **Or locally inside an air-gapped VM (no SSH at all):**
  ```bash
  ./install.sh --guest
  # or: ./wayport install-guest --local
  ```
> **Note:** `AF_VSOCK` is a raw transport socket, so `wayport-guest.service` must be installed once to listen on port `5000`. `install-guest` also attempts to enable systemd lingering (`loginctl enable-linger "$USER"`) so `wayport-guest.service` starts automatically on VM boot even when no user is logged in (if your distro restricts non-root `loginctl`, run `sudo loginctl enable-linger "$USER"` inside the VM once). After that, updates to `wayport-wrapper` and `wayport-tray-agent` (including changes to `USER CONFIGURATION`) are synced automatically from the host over `AF_VSOCK` without SSH.

---

## Installation

```bash
git clone https://github.com/Dreyk007/wayport.git
cd wayport
./install.sh
```

To completely remove `wayport` (binaries, systemd units, exported `.desktop` launchers, icons, cache, and runtime sockets on either host or guest):
```bash
./install.sh --uninstall
```

Verify your environment with the built-in diagnostic command:
```bash
wayport doctor --mode hybrid -t user@linux-vm.local
wayport doctor --mode vsock  --cid 3
wayport doctor --mode ssh    -t user@workstation.lan
```

---

## Usage

### Mode 1: `hybrid` (Default — SSH Control + VSOCK Data)
```bash
# List available GUI apps
wayport -t user@linux-vm.local list

# Export apps to KDE Plasma KRunner / Kickoff
wayport -t user@linux-vm.local export google-chrome org.kde.dolphin

# Run an application directly from terminal
wayport -t user@linux-vm.local run google-chrome --incognito

# Remove exported apps (or everything for this target)
wayport -t user@linux-vm.local remove google-chrome
wayport -t user@linux-vm.local remove --all
```

### Mode 2: `vsock` / `socket` (Pure `AF_VSOCK` — Zero SSH)
```bash
# List apps directly over AF_VSOCK (CID 3)
wayport --mode vsock --cid 3 list
# Equivalent alias & URI shorthand:
wayport --mode socket -t vsock://3 list

# Export with a custom human-readable badge label
wayport --mode vsock --cid 3 -t dev-vm export google-chrome code

# Run directly over AF_VSOCK
wayport --mode vsock --cid 3 run org.kde.dolphin
```

### Mode 3: `ssh` (Pure SSH — Remote Linux PCs, Workstations, VPN & WAN)

Run GUI applications seamlessly from **any remote Linux machine** over LAN, WireGuard/Tailscale VPN, or the Internet — no KVM, `virt-manager`, or `vhost-vsock` required. Wayland windows stream over an `ssh -R` Unix-socket `waypipe` tunnel, while System Tray icons, `DbusMenu` context menus, and desktop notifications stream over SSH `stdio` (`wayport-tray-agent --stdio`) with no extra open ports.

```bash
# 1. Verify connectivity and remote packages
wayport doctor --mode ssh -t user@workstation.lan

# 2. List available GUI applications on the remote machine
wayport --mode ssh -t user@workstation.lan list

# 3. Export apps into your local KDE Plasma launcher (KRunner / Kickoff)
#    and start the per-target SSH stdio tray & notification bridge
wayport --mode ssh -t user@workstation.lan export org.kde.kate code firefox

# 4. Run an application directly from terminal
#    - LAN / WireGuard / Tailscale: uses `lz4` compression by default
wayport --mode ssh -t user@workstation.lan run firefox
#    - Slow WAN / mobile internet: switch to `zstd` for lower bandwidth usage
wayport --mode ssh -t user@remote-pc.example.com -c zstd run firefox
#    - Fast gigabit LAN: optional `-c none` enables Wayland wire idle-inhibit masking
wayport --mode ssh -t user@workstation.lan -c none run firefox

# 5. Remove exported apps (and stop the SSH tray service with --all)
wayport --mode ssh -t user@workstation.lan remove firefox
wayport --mode ssh -t user@workstation.lan remove --all
```

> **Tips for remote Linux PCs & SSH config:**
> * **`~/.ssh/config` aliases work directly:** If you define a `Host work-pc` entry in `~/.ssh/config` (custom port, `ProxyJump`, IdentityFile), you can pass `-t work-pc` directly.
> * **Physical GPU on the remote PC:** In the `USER CONFIGURATION` section at the top of [`wayport`](wayport), set `VULKAN_USE_LVP = False` if you want Vulkan apps on the remote machine to use its hardware GPU ICD instead of software Lavapipe.
> * **Shared physical desktop:** If someone else is actively using the same Linux account on the remote PC's physical monitor at the same time, set `ENABLE_SINGLETON_TAKEOVER = False` and `ENABLE_PORTAL_ROUTING = False` so your remote session does not close their local browser/IDE or redirect their file dialogs.

### Monitor System Tray & Notification Bridge Logs
```bash
# Per-target systemd user service
journalctl --user -u 'wayport-tray-*.service' -f
```

---

## Configuration

All default settings are grouped in the `USER CONFIGURATION` section at the very top of [`wayport`](wayport) (default mode, target, ports, compression per mode, SSH timeouts, ignored tray processes, Chromium/Electron auto-detection & flags, XDG search paths, badge format).

You can also override settings at runtime with environment variables or CLI flags (`CLI > Env > Top-of-file defaults`):

| Environment Variable | CLI Flag | Default | Description |
| :--- | :--- | :--- | :--- |
| `WAYPORT_MODE` | `-m, --mode` | `hybrid` | Transport mode: `hybrid`, `vsock` (`socket`), `ssh` |
| `WAYPORT_TARGET` | `-t, --target` | `""` | Remote SSH target (`user@host`) or label / `vsock://<CID>` |
| `WAYPORT_CID` | `--cid` | `None` | Guest `AF_VSOCK` CID (`>= 3`) for `vsock` mode |
| `WAYPORT_COMPRESSION` | `-c, --compression` | `none` (`vsock`/`hybrid`), `lz4` (`ssh`) | `waypipe` compression (`none`, `lz4`, `zstd`) |
| `WAYPORT_NO_GPU` | `--no-gpu` / `--gpu` | `true` | Disable DMABUF forwarding in `waypipe` |
| `WAYPORT_MASK_IDLE_INHIBIT` | *(config)* | `true` | Mask `zwp_idle_inhibit_manager_v1` when uncompressed |
| `WAYPORT_MASK_LEGACY_WAYPIPE_PROTOCOLS` | *(config)* | `true` | Auto-mask incompatible 0.10+ Wayland globals when host runs legacy C `waypipe < 0.10` and uncompressed |
| `WAYPORT_CONTROL_PORT` | `--control-port` | `5000` | Guest `AF_VSOCK` control RPC port (`vsock` mode) |
| `WAYPORT_TRAY_PORT` | `--tray-port` | `5001` | Host `AF_VSOCK` tray bridge port (`hybrid`/`vsock`) |
| `WAYPORT_DEBUG` | `--debug` | `false` | Enable verbose bridge/session diagnostic logging |
| *(config: `AUTO_START_TRAY_ON_RUN`)* | `--no-tray` | `true` | Skip auto-starting/touching the host systemd tray service during `run` |

---

## Troubleshooting

* **`/dev/vhost-vsock` missing on host:** Run `sudo modprobe vhost_vsock`, or use `--mode ssh` if VSOCK is unavailable.
* **`Cannot connect to guest AF_VSOCK CID ... port 5000`:** Ensure the guest has a Virtio VSOCK device with matching CID, `wayport-guest.service` is active (`systemctl --user status wayport-guest.service`), and systemd lingering is enabled (`sudo loginctl enable-linger "$USER"`) so the user daemon starts on VM boot even with no logged-in user (re-install via `wayport install-guest`).
* **`ssh` mode exits immediately with forwarding error:** Check that `/etc/ssh/sshd_config` on the remote host allows `AllowStreamLocalForwarding yes`.
* **Tray icons do not appear:** Run `wayport doctor` to verify `python3-dbus` and `python3-gi` on both host and guest, then inspect `journalctl --user -u 'wayport-tray-*.service' -f`.
* **Using multiple VMs simultaneously in `hybrid`/`vsock` mode:** Pass a unique `--tray-port` (e.g. `5002`) for the second VM so each host tray daemon binds its own `AF_VSOCK` port.

---

## Contributing & Testing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines and test instructions:

```bash
python3 -m py_compile wayport
pytest -v tests/
shellcheck install.sh
```

---

## License

This project is licensed under the **GNU General Public License v3.0 or later** — see the [LICENSE](LICENSE) file for details.
