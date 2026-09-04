from __future__ import annotations

from typing import Any, Optional, Tuple

import paramiko
from scp import SCPClient


def connect_ssh(
    host: str,
    port: int,
    username: str,
    password: str,
    key_path: str,
    timeout: int,
    allow_agent: bool,
    look_for_keys: bool,
    overrides: Optional[dict[str, Any]] = None,
) -> paramiko.SSHClient:
    settings = dict(overrides or {})
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.WarningPolicy())
    client.connect(
        hostname=str(settings.get("host") or host),
        port=int(settings.get("port") or port),
        username=str(settings.get("username") or username),
        password=str(settings.get("password") or password) or None,
        key_filename=str(settings.get("key_path") or key_path) or None,
        timeout=timeout,
        allow_agent=allow_agent if "allow_agent" not in settings else bool(settings.get("allow_agent")),
        look_for_keys=look_for_keys if "look_for_keys" not in settings else bool(settings.get("look_for_keys")),
    )
    return client


def open_sftp(ssh: Any) -> Any:
    return ssh.open_sftp()


def open_scp(ssh: Any) -> Any:
    return SCPClient(ssh.get_transport())


def is_sftp_negotiation_error(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    if isinstance(exc, EOFError):
        return True
    return any(
        marker in text
        for marker in (
            "eof during negotiation",
            "open failed",
            "channel closed",
            "administratively prohibited",
            "subsystem request failed",
        )
    )


def run_ssh_command(ssh: Any, command: str, timeout: int) -> Tuple[int, str, str]:
    stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    try:
        stdin.close()
    except Exception:
        pass
    output = stdout.read().decode("utf-8", errors="replace")
    error = stderr.read().decode("utf-8", errors="replace")
    status = 0
    try:
        status = int(stdout.channel.recv_exit_status())
    except Exception:
        status = 0
    return status, output, error


def close_client(client: Any) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception:
        pass
