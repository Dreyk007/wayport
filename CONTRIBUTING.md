# Contributing to `wayport`

Contributions, bug reports, and feature requests are welcome!

## Development Setup

`wayport` is intentionally kept as a single standalone Python 3.10+ executable script (`wayport`) with zero third-party Python dependencies for the core CLI (`python3-dbus` and `python3-gi` are only needed at runtime by `tray-daemon` and `wayport-tray-agent`).

### Recommended Local Tools

```bash
sudo apt install -y python3-pytest shellcheck waypipe
```

## Running Automated Checks

Before opening a pull request, please verify that all checks pass:

```bash
# 1. Python syntax check
python3 -m py_compile wayport

# 2. Unit & protocol test suite
pytest -v tests/

# 3. Shellcheck on installer and embedded guest wrapper script
shellcheck install.sh
python3 - <<'EOF' | shellcheck -s bash -
import importlib.machinery, importlib.util, sys
loader = importlib.machinery.SourceFileLoader("wayport", "wayport")
spec = importlib.util.spec_from_loader("wayport", loader)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
loader.exec_module(mod)
print(mod.render_guest_wrapper())
EOF
```

## Guidelines

* Keep all user-configurable defaults inside the `USER CONFIGURATION` section at the top of `wayport`.
* Preserve compatibility across all three transport modes (`hybrid`, `vsock`/`socket`, and `ssh`).
* Never interpolate untrusted strings (desktop entry fields, icon names, filenames) into shell or inline Python code — pass them via `sys.argv` or JSON-RPC.
* Add or update unit tests in `tests/test_wayport.py` for any bug fix or new feature.
* Update `README.md` and `ARCHITECTURE.md` when changing user-facing behavior or wire protocols.

## License

By contributing, you agree that your contributions will be licensed under the **GNU General Public License v3.0 or later** (`GPL-3.0-or-later`).
