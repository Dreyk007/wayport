---
name: Bug report
about: Report a bug or unexpected behavior in wayport
title: "[BUG] "
labels: bug
---

### Description
A clear and concise description of what happened.

### Transport Mode & Command
- **Mode:** `hybrid` / `vsock` / `ssh`
- **Command run:**
  ```bash
  wayport ...
  ```

### Environment
- **Host OS & KDE Plasma version:** (e.g. Ubuntu 24.04 / KDE Plasma 6.x)
- **Guest OS:** (e.g. Debian Testing, headless or desktop)
- **Host `waypipe --version`:**
- **Guest `waypipe --version`:**
- **`wayport --version`:**

### Output of `wayport doctor`
```text
Paste output here
```

### Logs / Steps to Reproduce
Include relevant terminal output or `journalctl --user -u 'wayport-tray-*.service' -n 50`.
