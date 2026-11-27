# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit and protocol tests for wayport."""

from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "wayport"


def load_wayport() -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader("wayport_mod", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader("wayport_mod", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def app():
    return load_wayport()


# ------------------------------------------------------------------------------
# 1. Configuration & CLI Parsing
# ------------------------------------------------------------------------------

def test_mode_normalization_and_defaults(app, monkeypatch):
    for var in (
        "WAYPORT_MODE",
        "WAYPORT_TARGET",
        "WAYPORT_CID",
        "WAYPORT_COMPRESSION",
        "WAYPORT_NO_GPU",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg_hybrid = app.build_runtime_config(vm_target="alice@vm1.local")
    assert cfg_hybrid.mode == "hybrid"
    assert cfg_hybrid.compression == app.WAYPIPE_COMPRESSION_VSOCK
    assert cfg_hybrid.vm_label == "vm1.local"
    assert cfg_hybrid.systemd_service_name == "wayport-tray-vm1.local.service"
    assert cfg_hybrid.effective_mask_idle_inhibit is True

    cfg_ssh = app.build_runtime_config(mode="ssh", vm_target="alice@remote.lan")
    assert cfg_ssh.mode == "ssh"
    assert cfg_ssh.compression == app.WAYPIPE_COMPRESSION_SSH
    assert cfg_ssh.effective_mask_idle_inhibit is (app.WAYPIPE_COMPRESSION_SSH == "none")

    cfg_socket_alias = app.build_runtime_config(mode="socket", vsock_cid=4)
    assert cfg_socket_alias.mode == "vsock"
    assert cfg_socket_alias.vsock_cid == 4
    assert cfg_socket_alias.vm_label == "cid-4"

    cfg_uri = app.build_runtime_config(vm_target="vsock://7")
    assert cfg_uri.mode == "vsock"
    assert cfg_uri.vsock_cid == 7
    assert cfg_uri.vm_label == "cid-7"


def test_env_and_cli_precedence(app, monkeypatch):
    monkeypatch.setenv("WAYPORT_MODE", "ssh")
    monkeypatch.setenv("WAYPORT_TARGET", "bob@envhost")
    monkeypatch.setenv("WAYPORT_COMPRESSION", "zstd")

    cfg_env = app.build_runtime_config()
    assert cfg_env.mode == "ssh"
    assert cfg_env.vm_target == "bob@envhost"
    assert cfg_env.compression == "zstd"

    cfg_cli = app.build_runtime_config(mode="hybrid", vm_target="carol@clihost", compression="none")
    assert cfg_cli.mode == "hybrid"
    assert cfg_cli.vm_target == "carol@clihost"
    assert cfg_cli.compression == "none"


def test_vsock_invalid_cid_rejected(app):
    with pytest.raises(ValueError, match="CID must be >= 3"):
        app.build_runtime_config(mode="vsock", vsock_cid=2)

    with pytest.raises(ValueError, match="requires a guest CID"):
        app.build_runtime_config(mode="vsock", vm_target="my-vm")


def test_cli_run_preserves_guest_flags(app):
    parser = app.build_arg_parser()
    args = parser.parse_args(
        ["--mode", "ssh", "--target", "u@h", "run", "google-chrome", "--incognito", "--ozone-platform=wayland", "--mode=app"]
    )
    assert args.mode == "ssh"
    assert args.target == "u@h"
    assert args.command == "run"
    assert args.cmd == ["google-chrome", "--incognito", "--ozone-platform=wayland", "--mode=app"]

    args_after = parser.parse_args(["doctor", "--mode", "vsock", "--cid", "3"])
    assert args_after.command == "doctor"
    assert args_after.mode == "vsock"
    assert args_after.cid == 3

    args_short = parser.parse_args(["list", "--mode", "ssh", "-t", "alice@pc.lan"])
    assert args_short.command == "list"
    assert args_short.target == "alice@pc.lan"

    # Legacy --vm and --host flags are intentionally dropped
    with pytest.raises(SystemExit):
        parser.parse_args(["list", "--vm", "alice@pc.lan"])
    with pytest.raises(SystemExit):
        parser.parse_args(["list", "--host", "alice@pc.lan"])


# ------------------------------------------------------------------------------
# 2. Boundary-Safe Idle-Inhibit Stream Masking & Socket Closing
# ------------------------------------------------------------------------------

@pytest.mark.parametrize("split_at", list(range(1, 27)))
def test_pump_socket_stream_masks_across_every_chunk_boundary(app, split_at):
    needle = app.IDLE_INHIBIT_NEEDLE
    replacement = app.IDLE_INHIBIT_REPLACEMENT
    prefix = b"\x00\x01\x02wl_registry"
    suffix = b"\x00\x00trailing_bytes"
    part1 = prefix + needle[:split_at]
    part2 = needle[split_at:] + suffix

    src_w, src_r = socket.socketpair()
    dst_r, dst_w = socket.socketpair()

    t = threading.Thread(
        target=app.pump_socket_stream,
        args=(src_r, dst_w),
        kwargs={"mask_idle_inhibit": True, "chunk_size": 1024},
        daemon=True,
    )
    t.start()

    src_w.sendall(part1)
    # Wait briefly to ensure part1 is processed separately from part2
    src_w.sendall(part2)
    src_w.shutdown(socket.SHUT_WR)

    received = bytearray()
    while True:
        chunk = dst_r.recv(4096)
        if not chunk:
            break
        received.extend(chunk)

    t.join(timeout=2.0)
    assert not t.is_alive()
    assert bytes(received) == prefix + replacement + suffix

    for s in (src_w, src_r, dst_r, dst_w):
        s.close()


def test_pump_socket_stream_single_byte_chunks_and_trailing_prefix_eof(app):
    payload = b"abc" + app.IDLE_INHIBIT_NEEDLE + b"xyz" + app.IDLE_INHIBIT_NEEDLE[:5]
    expected = b"abc" + app.IDLE_INHIBIT_REPLACEMENT + b"xyz" + app.IDLE_INHIBIT_NEEDLE[:5]

    src_w, src_r = socket.socketpair()
    dst_r, dst_w = socket.socketpair()

    t = threading.Thread(
        target=app.pump_socket_stream,
        args=(src_r, dst_w),
        kwargs={"mask_idle_inhibit": True, "chunk_size": 1},
        daemon=True,
    )
    t.start()

    src_w.sendall(payload)
    src_w.shutdown(socket.SHUT_WR)

    received = bytearray()
    while True:
        chunk = dst_r.recv(4096)
        if not chunk:
            break
        received.extend(chunk)

    t.join(timeout=2.0)
    assert bytes(received) == expected
    for s in (src_w, src_r, dst_r, dst_w):
        s.close()


def test_bridge_socket_pair_closes_fds(app):
    g_ext, g_int = socket.socketpair()
    h_ext, h_int = socket.socketpair()

    t1, t2 = app.bridge_socket_pair(g_int, h_int, mask_idle_inhibit=True)

    g_ext.sendall(b"hello_from_guest")
    assert h_ext.recv(1024) == b"hello_from_guest"

    h_ext.sendall(app.IDLE_INHIBIT_NEEDLE)
    h_ext.shutdown(socket.SHUT_WR)
    g_ext.shutdown(socket.SHUT_WR)

    assert g_ext.recv(1024) == app.IDLE_INHIBIT_REPLACEMENT
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    # Both internal sockets must be closed (fileno() == -1)
    assert g_int.fileno() == -1
    assert h_int.fileno() == -1

    g_ext.close()
    h_ext.close()


def test_pump_socket_stream_flushes_trailing_prefix_without_deadlock(app):
    """A Wayland packet ending in `b'z'` (prefix of zwp_idle_inhibit_manager_v1) must not stall."""
    src_w, src_r = socket.socketpair()
    dst_r, dst_w = socket.socketpair()
    dst_r.settimeout(1.0)

    t = threading.Thread(
        target=app.pump_socket_stream,
        args=(src_r, dst_w),
        kwargs={"mask_idle_inhibit": True, "flush_timeout": 0.01},
        daemon=True,
    )
    t.start()

    # Keep src_w open (simulating a live roundtrip waiting for reply) and send bytes ending in 'z'
    packet = b"\x01\x00\x00\x00hello_z"
    src_w.sendall(packet)

    received = bytearray()
    while len(received) < len(packet):
        chunk = dst_r.recv(1024)
        assert chunk, "Unexpected EOF before receiving trailing 'z'"
        received.extend(chunk)
    assert bytes(received) == packet

    src_w.shutdown(socket.SHUT_WR)
    t.join(timeout=2.0)
    assert not t.is_alive()
    for s in (src_w, src_r, dst_r, dst_w):
        s.close()


def test_legacy_waypipe_protocol_masking_and_version_detection(app):
    assert app.parse_waypipe_version("waypipe 0.8.6\n") == (0, 8, 6)
    assert app.parse_waypipe_version("waypipe 0.11.2\nfeatures:\n  lz4: true\n") == (0, 11, 2)
    assert app.parse_waypipe_version("unknown") is None

    cfg = app.build_runtime_config(mode="vsock", vsock_cid=3, compression="none")
    legacy_reps = app.build_stream_replacements(cfg, host_waypipe_version=(0, 8, 6))
    modern_reps = app.build_stream_replacements(cfg, host_waypipe_version=(0, 11, 2))

    assert len(modern_reps) == 1
    assert len(legacy_reps) == 1 + len(app.LEGACY_WAYPIPE_PROTOCOL_MASKS)

    # Framed wl_registry.global entries must be masked across chunk boundaries,
    # while unframed plain text containing "wl_fixes" must remain untouched.
    framed_edc = b"\x1c\x00\x00\x00ext_data_control_manager_v1\x00"
    expected_edc = b"\x1c\x00\x00\x00ext_data_control_disabledv1\x00"
    framed_fixes = b"\x09\x00\x00\x00wl_fixes\x00"
    expected_fixes = b"\x09\x00\x00\x00wl_fix00\x00"
    plain_text = b"clipboard says wl_fixes is cool"
    payload = framed_edc + plain_text + framed_fixes

    src_w, src_r = socket.socketpair()
    dst_r, dst_w = socket.socketpair()
    t = threading.Thread(
        target=app.pump_socket_stream,
        args=(src_r, dst_w),
        kwargs={"replacements": legacy_reps, "chunk_size": 7},
        daemon=True,
    )
    t.start()
    src_w.sendall(payload)
    src_w.shutdown(socket.SHUT_WR)

    received = bytearray()
    while True:
        chunk = dst_r.recv(4096)
        if not chunk:
            break
        received.extend(chunk)
    t.join(timeout=2.0)

    assert bytes(received) == expected_edc + plain_text + expected_fixes
    for s in (src_w, src_r, dst_r, dst_w):
        s.close()


def test_vsock_session_handle_ignores_stdout_broken_pipe_and_reports_unexpected_eof(app, monkeypatch):
    # 1. BrokenPipeError on host stdout must NOT terminate the VSOCK control session
    c_sock, s_sock = socket.socketpair()

    class BrokenStream:
        def write(self, _text):
            raise BrokenPipeError("pipe closed")

        def flush(self):
            pass

    monkeypatch.setattr(app.sys, "stdout", BrokenStream())
    handle = app.VsockSessionHandle(c_sock, forward_output=True)
    s_sock.sendall(b'{"event":"stdout","data":"hello\\n"}\n')
    s_sock.sendall(b'{"event":"exit","code":0}\n')
    assert handle.wait(timeout=2.0) == 0
    assert handle.disconnect_reason is None
    s_sock.close()

    # 2. Abrupt EOF without exit event records disconnect_reason and returns non-zero
    c_sock2, s_sock2 = socket.socketpair()
    handle2 = app.VsockSessionHandle(c_sock2, forward_output=False)
    s_sock2.close()
    assert handle2.wait(timeout=2.0) == 1
    assert handle2.disconnect_reason is not None
    assert "EOF" in handle2.disconnect_reason


def test_sigpipe_not_set_to_sig_dfl_in_entrypoint():
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "signal.SIG_DFL" not in src
    assert "signal.signal(signal.SIGPIPE, signal.SIG_IGN)" in src


# ------------------------------------------------------------------------------
# 3. Desktop File Transformation & Exec Rewriting
# ------------------------------------------------------------------------------

def test_rewrite_exec_line_preserves_quotes_mode_and_percent(app):
    cfg = app.build_runtime_config(mode="vsock", vsock_cid=3, vm_target="dev-vm")
    rewritten = app.rewrite_exec_line('env FOO=1 "/opt/My App/bin" --title="100%% Done" "%U"', cfg)

    assert "--mode vsock" in rewritten
    assert "--cid 3" in rewritten
    assert "--target dev-vm" in rewritten
    assert "run --" in rewritten
    assert '"/opt/My App/bin"' in rewritten
    assert '"--title=100%% Done"' in rewritten
    assert rewritten.endswith(" %U")
    assert "FOO=1" not in rewritten

    # Qoder-style `--open-file %U` must not leave dangling `--open-file`
    qoder_exec = app.rewrite_exec_line("/opt/Qoder/qoder --open-file %U", cfg)
    assert "--open-file" not in qoder_exec
    assert "/opt/Qoder/qoder %U" in qoder_exec

    # `%F` file-list code is preserved when `%U` is absent
    file_exec = app.rewrite_exec_line("/usr/bin/kate %F", cfg)
    assert file_exec.endswith(" %F")


def test_broken_pipe_handled_cleanly(app, monkeypatch):
    def raise_bpe(*args, **kwargs):
        raise BrokenPipeError()

    monkeypatch.setattr(app, "list_guest_applications", raise_bpe)
    rc = app.main(["--mode", "ssh", "--target", "u@h", "list"])
    assert rc == 0


def test_find_conflicting_active_tray_service(app, monkeypatch, tmp_path):
    (tmp_path / "wayport-tray-other.service").write_text(
        "[Unit]\nDescription=wayport (other, mode=hybrid)\n"
        "[Service]\nExecStart=/usr/local/bin/wayport --mode hybrid --target u@other tray-daemon\n",
        encoding="utf-8",
    )
    (tmp_path / "wayport-tray-sshonly.service").write_text(
        "[Unit]\nDescription=wayport (sshonly, mode=ssh)\n"
        "[Service]\nExecStart=/usr/local/bin/wayport --mode ssh --target u@sshonly tray-daemon\n",
        encoding="utf-8",
    )

    class DummyRes:
        returncode = 0

    monkeypatch.setattr(app.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **kw: DummyRes())

    cfg_vsock = app.build_runtime_config(mode="vsock", vsock_cid=3)
    assert app.find_conflicting_active_tray_service(cfg_vsock, systemd_dir=tmp_path) == "wayport-tray-other.service"

    cfg_other_port = app.build_runtime_config(mode="vsock", vsock_cid=3, tray_port=5002)
    assert app.find_conflicting_active_tray_service(cfg_other_port, systemd_dir=tmp_path) is None

    cfg_ssh = app.build_runtime_config(mode="ssh", vm_target="u@box")
    assert app.find_conflicting_active_tray_service(cfg_ssh, systemd_dir=tmp_path) is None


def test_transform_desktop_file_cleans_fields_and_avoids_double_badge(app):
    cfg = app.build_runtime_config(mode="ssh", vm_target="user@box.lan")
    raw = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=My App\n"
        "Name[ru]=Моё приложение [box.lan]\n"
        "TryExec=/usr/bin/myapp\n"
        "Path=/opt/guest-only-dir\n"
        "DBusActivatable=true\n"
        "Icon=myapp-icon\n"
        "Exec=/usr/bin/myapp --flag %U\n"
        "\n"
        "[Desktop Action NewWindow]\n"
        "Name=New Window\n"
        "Icon=action-icon\n"
        "Exec=/usr/bin/myapp --new-window\n"
    )
    assert app.extract_desktop_entry_icon(raw) == "myapp-icon"

    out = app.transform_desktop_file(raw, cfg, "/home/host/.local/share/icons/wayport/vm-box.lan-myapp.svg")
    assert "TryExec=" not in out
    assert "Path=" not in out
    assert "DBusActivatable=false" in out
    assert "Name=My App [box.lan]" in out
    assert "Name[ru]=Моё приложение [box.lan]" in out
    assert "[box.lan] [box.lan]" not in out
    assert "Icon=/home/host/.local/share/icons/wayport/vm-box.lan-myapp.svg" in out
    # Action section icon & name remain untouched; Action Exec IS rewritten
    assert "Icon=action-icon" in out
    assert "Name=New Window\n" in out
    assert out.count("--mode ssh --target user@box.lan run -- /usr/bin/myapp") == 2


# ------------------------------------------------------------------------------
# 4. Security: Theme Sync Path Traversal & Safe SSH Arg Passing
# ------------------------------------------------------------------------------

def test_sync_theme_files_safe_blocks_traversal_and_absolute(app, tmp_path):
    target_dir = tmp_path / "tray-cache"
    good_b64 = base64.b64encode(b"PNGDATA").decode("ascii")
    evil_b64 = base64.b64encode(b"EVIL").decode("ascii")

    out_dir = app.sync_theme_files_safe(
        target_dir,
        {
            "ok/icon.png": good_b64,
            "../escape.png": evil_b64,
            "/tmp/should_never_write_wayport_test.png": evil_b64,
        },
    )

    assert Path(out_dir) == target_dir.resolve()
    assert (target_dir / "ok" / "icon.png").read_bytes() == b"PNGDATA"
    assert not (tmp_path / "escape.png").exists()
    assert not Path("/tmp/should_never_write_wayport_test.png").exists()


def test_ssh_transport_raises_transport_error_not_sys_exit(app, monkeypatch):
    cfg = app.build_runtime_config(mode="hybrid", vm_target="user@offline.local")
    transport = app.SSHControlTransport(cfg)

    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(255, args[0], stderr=b"Connection refused")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(app.TransportError, match="Connection refused"):
        transport.run_command("echo hi", check=True)


def test_ssh_fetch_icon_uses_argv_safely(app, monkeypatch):
    cfg = app.build_runtime_config(mode="hybrid", vm_target="user@vm.local")
    transport = app.SSHControlTransport(cfg)
    captured: list[str] = []

    def fake_run_command(remote_cmd, **kwargs):
        captured.append(remote_cmd)
        return b"EXT:.svg\n<svg/>"

    monkeypatch.setattr(transport, "run_command", fake_run_command)
    tricky_name = "google-chrome'; $(id) \""
    res = transport.fetch_icon(tricky_name)

    assert res == (".svg", b"<svg/>")
    assert len(captured) == 1
    # Verify python code reads sys.argv[2] instead of interpolating icon_name
    assert "icon = sys.argv[2]" in captured[0]


def test_ssh_remote_python_commands_execute_locally(app, monkeypatch, tmp_path):
    """Executes the exact remote shell/python3 -c strings generated by SSHControlTransport."""
    user_apps = tmp_path / "user-apps"
    sys_apps = tmp_path / "sys-apps"
    icons_dir = tmp_path / "icons"
    user_apps.mkdir()
    sys_apps.mkdir()
    icons_dir.mkdir()

    (sys_apps / "demo.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=System Demo\nExec=/usr/bin/demo\n",
        encoding="utf-8",
    )
    (user_apps / "demo.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=User Override Demo\nExec=/usr/local/bin/demo\n",
        encoding="utf-8",
    )
    (sys_apps / "hidden.desktop").write_text(
        "[Desktop Entry]\nType=Application\nNoDisplay=true\nName=Hidden\n",
        encoding="utf-8",
    )
    (icons_dir / "google-chrome.svg").write_bytes(b"<svg>chrome</svg>")

    monkeypatch.setattr(app, "DESKTOP_APP_SEARCH_DIRS", (str(user_apps), str(sys_apps)))
    monkeypatch.setattr(app, "ICON_SEARCH_DIRS", (str(icons_dir),))

    cfg = app.build_runtime_config(mode="hybrid", vm_target="user@vm.local")
    transport = app.SSHControlTransport(cfg)

    def exec_locally(remote_cmd, *, stdin_bytes=None, check=True):
        res = subprocess.run(
            ["bash", "-c", remote_cmd],
            input=stdin_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )
        return res.stdout

    monkeypatch.setattr(transport, "run_command", exec_locally)

    apps = transport.list_apps()
    assert apps == [("demo", "User Override Demo")]

    desk = transport.read_desktop_file("demo")
    assert desk is not None
    assert "Name=User Override Demo" in desk
    assert "System Demo" not in desk
    assert transport.read_desktop_file("../etc/passwd") is None

    icon = transport.fetch_icon("google-chrome")
    assert icon == (".svg", b"<svg>chrome</svg>")
    assert transport.fetch_icon("nonexistent-icon") is None


# ------------------------------------------------------------------------------
# 5. Embedded Guest Scripts Syntax, Shellcheck, ARGB32 & VSOCK JSON-RPC Daemon
# ------------------------------------------------------------------------------

def test_embedded_guest_scripts_compile_and_shellcheck(app):
    tray_src = app.render_guest_tray_agent()
    daemon_src = app.render_guest_control_daemon()
    wrapper_src = app.render_guest_wrapper()

    compile(tray_src, "wayport-tray-agent", "exec")
    compile(daemon_src, "wayport-daemon", "exec")

    res_bash = subprocess.run(["bash", "-n"], input=wrapper_src, text=True, capture_output=True)
    assert res_bash.returncode == 0, res_bash.stderr

    if shutil.which("shellcheck"):
        res_sc = subprocess.run(["shellcheck", "-s", "bash", "-"], input=wrapper_src, text=True, capture_output=True)
        assert res_sc.returncode == 0, res_sc.stdout + res_sc.stderr


def test_vectorized_rgba_to_argb32_matches_reference(app):
    tray_src = app.render_guest_tray_agent()
    # Extract rgba_to_argb32 directly from rendered script without importing dbus/gi
    start = tray_src.index("def rgba_to_argb32(")
    end = tray_src.index("def file_to_argb32_pixmap(")
    ns: dict = {}
    exec(tray_src[start:end], ns)
    rgba_to_argb32 = ns["rgba_to_argb32"]

    # 2x2 RGBA with rowstride padding (rowstride = 10 > 2*4=8)
    row0 = bytes([10, 20, 30, 40, 50, 60, 70, 80, 99, 99])
    row1 = bytes([11, 22, 33, 255, 44, 55, 66, 0, 88, 88])
    out = rgba_to_argb32(row0 + row1, 2, 2, 10, 4, True)
    expected = bytes([
        40, 10, 20, 30, 80, 50, 60, 70,
        255, 11, 22, 33, 0, 44, 55, 66,
    ])
    assert out == expected

    # 2x1 RGB (no alpha)
    rgb = bytes([1, 2, 3, 4, 5, 6])
    out_rgb = rgba_to_argb32(rgb, 2, 1, 6, 3, False)
    assert out_rgb == bytes([255, 1, 2, 3, 255, 4, 5, 6])


def test_guest_control_daemon_json_rpc_over_socketpair(app, tmp_path):
    daemon_src = app.render_guest_control_daemon(control_port=5000)
    ns: dict = {"__name__": "wayport_daemon_test"}
    exec(compile(daemon_src, "wayport-daemon", "exec"), ns)

    apps_dir = tmp_path / "apps"
    icons_dir = tmp_path / "icons"
    bin_dir = tmp_path / "bin"
    apps_dir.mkdir()
    icons_dir.mkdir()
    bin_dir.mkdir()

    (apps_dir / "demo.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Demo App\nExec=/usr/bin/demo\nIcon=demo-icon\n",
        encoding="utf-8",
    )
    (icons_dir / "demo-icon.svg").write_bytes(b"<svg>ok</svg>")

    ns["DESKTOP_APP_SEARCH_DIRS"] = [str(apps_dir)]
    ns["ICON_SEARCH_DIRS"] = [str(icons_dir)]
    ns["GUEST_BIN_DIR"] = bin_dir

    def rpc_call(payload: dict, peer_cid: int = 2) -> dict:
        c_sock, s_sock = socket.socketpair()
        c_sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        t = threading.Thread(target=ns["handle_client"], args=(s_sock, peer_cid), daemon=True)
        t.start()
        with c_sock.makefile("r", encoding="utf-8") as r:
            resp = json.loads(r.readline())
        c_sock.close()
        t.join(timeout=2.0)
        return resp

    # 1. Unauthorized non-host CID must be rejected
    unauth = rpc_call({"op": "ping"}, peer_cid=99)
    assert unauth["ok"] is False
    assert "Unauthorized" in unauth["error"]

    # 2. Authorized host CID (2) succeeds for ping, list_apps, get_desktop, get_icon, sync_scripts
    ping = rpc_call({"op": "ping"}, peer_cid=2)
    assert ping["ok"] is True
    assert ping["version"] == app.__version__

    listed = rpc_call({"op": "list_apps"}, peer_cid=2)
    assert listed["ok"] is True
    assert listed["apps"] == [{"id": "demo", "name": "Demo App"}]

    desk = rpc_call({"op": "get_desktop", "app_id": "demo"}, peer_cid=2)
    assert desk["ok"] is True
    assert "Name=Demo App" in desk["content"]

    icon = rpc_call({"op": "get_icon", "icon_name": "demo-icon"}, peer_cid=2)
    assert icon["ok"] is True
    assert icon["ext"] == ".svg"
    assert base64.b64decode(icon["b64"]) == b"<svg>ok</svg>"

    # 3. sync_scripts only allows allowlisted filenames
    sync = rpc_call(
        {
            "op": "sync_scripts",
            "scripts": {
                "wayport-wrapper": "#!/bin/sh\necho ok\n",
                "../../evil.sh": "bad",
            },
        },
        peer_cid=2,
    )
    assert sync["ok"] is True
    assert sync["updated"] == ["wayport-wrapper"]
    assert (bin_dir / "wayport-wrapper").is_file()
    assert os.access(bin_dir / "wayport-wrapper", os.X_OK)
    assert not (tmp_path / "evil.sh").exists()


def test_guest_daemon_systemd_unit_and_linger_bootstrap(app, monkeypatch, tmp_path):
    unit = app.build_guest_daemon_systemd_unit(control_port=5055)
    assert "Wants=dbus.socket" in unit
    assert "After=dbus.socket" in unit
    assert "After=default.target" not in unit
    assert "WantedBy=default.target" in unit
    assert "TimeoutStopSec=5" in unit
    assert "--port 5055" in unit

    calls: list[list[str]] = []

    class DummyRes:
        def __init__(self, stdout: str = "") -> None:
            self.returncode = 0
            self.stdout = stdout

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["loginctl", "show-user"]:
            return DummyRes("Linger=yes\n")
        return DummyRes()

    monkeypatch.setattr(app, "GUEST_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setattr(app.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(app.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(app.subprocess, "run", fake_run)

    cfg = app.build_runtime_config(mode="vsock", vsock_cid=3)
    app.install_guest_components(cfg, local=True)

    assert (tmp_path / "bin" / "wayport-daemon").is_file()
    assert (tmp_path / ".config" / "systemd" / "user" / "wayport-guest.service").is_file()
    assert any(c[:2] == ["loginctl", "enable-linger"] for c in calls)
    assert any(c[:3] == ["systemctl", "--user", "enable"] for c in calls)


def test_install_sh_uninstall_cleans_all_artifacts_safely(tmp_path):
    fake_home = tmp_path / "home"
    fake_runtime = tmp_path / "runtime"
    fake_bin_path = tmp_path / "stub_path"
    for d in (
        fake_home / ".local" / "bin",
        fake_home / ".local" / "share" / "applications",
        fake_home / ".local" / "share" / "icons" / "wayport",
        fake_home / ".cache" / "wayport",
        fake_home / ".config" / "systemd" / "user",
        fake_runtime,
        fake_bin_path,
    ):
        d.mkdir(parents=True, exist_ok=True)

    # Create stub systemctl/update-desktop-database/kbuildsycoca6 so we never touch real host systemd
    for stub in ("systemctl", "update-desktop-database", "kbuildsycoca6"):
        p = fake_bin_path / stub
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)

    # Populate wayport artifacts (host + guest)
    for name in ("wayport", "wayport-wrapper", "wayport-tray-agent", "wayport-daemon"):
        (fake_home / ".local" / "bin" / name).write_text("#!/bin/sh\n", encoding="utf-8")
    (fake_home / ".local" / "share" / "applications" / "wayport-cid-3-qoder.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    (fake_home / ".local" / "share" / "icons" / "wayport" / "wayport-cid-3-qoder.png").write_bytes(b"PNG")
    (fake_home / ".cache" / "wayport" / "scripts.json").write_text("{}", encoding="utf-8")
    (fake_home / ".config" / "systemd" / "user" / "wayport-tray-cid-3.service").write_text("[Unit]\n", encoding="utf-8")
    (fake_home / ".config" / "systemd" / "user" / "wayport-guest.service").write_text("[Unit]\n", encoding="utf-8")
    (fake_runtime / "wayport-client-123.sock").write_text("", encoding="utf-8")
    (fake_runtime / "wayport-test.stamp").write_text("", encoding="utf-8")

    # Unrelated user files must NOT be touched
    unrelated_bin = fake_home / ".local" / "bin" / "other-tool"
    unrelated_desktop = fake_home / ".local" / "share" / "applications" / "firefox.desktop"
    unrelated_unit = fake_home / ".config" / "systemd" / "user" / "other.service"
    unrelated_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    unrelated_desktop.write_text("[Desktop Entry]\n", encoding="utf-8")
    unrelated_unit.write_text("[Unit]\n", encoding="utf-8")

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["XDG_RUNTIME_DIR"] = str(fake_runtime)
    env["PATH"] = f"{fake_bin_path}:{env.get('PATH', '')}"

    res = subprocess.run(
        [str(REPO_ROOT / "install.sh"), "--uninstall"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr

    # All wayport files removed
    for name in ("wayport", "wayport-wrapper", "wayport-tray-agent", "wayport-daemon"):
        assert not (fake_home / ".local" / "bin" / name).exists()
    assert not (fake_home / ".local" / "share" / "applications" / "wayport-cid-3-qoder.desktop").exists()
    assert not (fake_home / ".local" / "share" / "icons" / "wayport").exists()
    assert not (fake_home / ".cache" / "wayport").exists()
    assert not (fake_home / ".config" / "systemd" / "user" / "wayport-tray-cid-3.service").exists()
    assert not (fake_home / ".config" / "systemd" / "user" / "wayport-guest.service").exists()
    assert not (fake_runtime / "wayport-client-123.sock").exists()
    assert not (fake_runtime / "wayport-test.stamp").exists()

    # Unrelated files untouched
    assert unrelated_bin.exists()
    assert unrelated_desktop.exists()
    assert unrelated_unit.exists()
