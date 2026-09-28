"""Running a local command or an SSH operation, with every failure recorded and redacted."""

from __future__ import annotations

from pathlib import Path
import logging
import subprocess

from control_plane.provider_adapters.contracts import ProviderError
from controller_runtime.command_env import command_environment
from . import connection_env, provider_http


logger = logging.getLogger("severino.controller")


# Work this pass could not finish, in the order it failed.
#
# A step is not an operation. An operation that fails is reported to HQ and
# waits there to be looked at; a step inside a sweep fails, is logged on the
# machine that ran it, and leaves no mark anywhere HQ can see. A step failing
# on every pass and a step that never runs look the same from HQ, and one of
# them is a fault.
#
# In memory for the life of one pass, which is one short-lived container, and
# reported once at the end of it.
_STEP_FAILURES: list[dict[str, str]] = []


def step_failures() -> tuple[dict[str, str], ...]:
    """What this pass could not finish, for the report at the end of it."""

    return tuple(_STEP_FAILURES)


def _record_step_failure(step: str, subject: str, reason: str) -> None:
    _STEP_FAILURES.append({"step": step, "subject": subject, "reason": reason})


def _redacted(text: str, env: dict[str, str] | None) -> str:
    """Whatever a failing tool said, with the credential it was given struck out.

    A tool that echoes back what it was handed would otherwise put the value in
    the journal, and this module is the only place that knows what to look for.
    """

    for value in (env or {}).values():
        if value:
            text = text.replace(value, "[redacted]")
    return text


def run_command(
    command: list[str],
    *,
    input_bytes: bytes | None = None,
    # Required, so every failure names what failed.
    step: str,
    env: dict[str, str] | None = None,
    subject: str = "",
) -> bytes:
    """Run a subprocess, saying which step failed rather than which module ran it.

    `step` names the thing that failed: certbot, openssl, or an SSH call to a
    host.

    The subprocess's own output never reaches the result: it carries paths and
    remote messages that belong in an operator's log rather than in a provider
    result that reaches an API client. What HQ is told is the step that failed;
    why, when HQ needs to know, comes from a check written for it (the ACME
    ownership check, the cPanel site plan), which says only what it means to.

    The log does get what the tool said: its last line of stderr, credentials
    struck out, in the message itself, because the controller's plain formatter
    drops `extra`. The full stderr stays in `extra` for a structured handler.

    ``env`` is added to this process's environment for the one call, for a tool
    that takes its credential that way. It is not an argument, because an
    argument list is readable by anything else on the machine, and its values are
    struck out of the stderr logged below: a tool that echoed back what it was
    given would otherwise put the credential in the journal, and this function is
    the only place that knows the value to look for.
    """

    try:
        result = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=180,
            env=command_environment(env),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # The tool was not there, or never returned. There is no exit code to
        # report, so the exception's type is what names the cause (a missing
        # binary is a FileNotFoundError) and it goes in the message, which is
        # the part the controller's plain formatter prints.
        provider_http.logger.warning(
            "controller step failed: %s (%s)",
            step,
            type(exc).__name__,
            extra={
                "event": "controller.step.failed",
                "step": step,
                "exception": type(exc).__name__,
                # Held back from the message on the same terms as stderr below:
                # it names the executable and can carry a path.
                "detail": _redacted(str(exc), env)[:2000],
            },
        )
        _record_step_failure(step, subject, type(exc).__name__)
        raise ProviderError(f"{step} could not complete.") from exc
    if result.returncode:
        stderr = _redacted(result.stderr.decode("utf-8", "replace"), env)
        said = _last_line(stderr)
        # The step and what the tool said both go in the message: the
        # controller's plain formatter prints the message and drops the rest.
        provider_http.logger.warning(
            "controller step failed: %s (exit %s)%s",
            step,
            result.returncode,
            f": {said}" if said else "",
            extra={
                "event": "controller.step.failed",
                "step": step,
                "exit_code": result.returncode,
                "stderr": stderr[:2000],
            },
        )
        _record_step_failure(step, subject, f"exit {result.returncode}")
        raise ProviderError(f"{step} failed.")
    return result.stdout


# Long enough for a path and an errno; short enough to sit in a status line.
_SAID_LIMIT = 240


def _last_line(stderr: str) -> str:
    """The line a tool's failure is usually explained by: its last one."""

    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[-1]
    return line if len(line) <= _SAID_LIMIT else line[: _SAID_LIMIT - 1] + "…"


def run_ssh(connection_ref: str, operation: str, payload: bytes | None = None) -> bytes:
    transport = connection_env.ssh_target(connection_ref)
    ssh_dir = Path(provider_http.required("HQ_CONTROLLER", "SSH_DIR"))
    command = [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={ssh_dir / 'known_hosts'}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-i",
        str(ssh_dir / connection_ref),
        "-p",
        str(transport["port"]),
        f"{transport['user']}@{transport['host']}",
        operation,
    ]
    return run_command(
        command,
        input_bytes=payload,
        step=f"SSH {operation} for {connection_ref}",
        subject=connection_ref,
    )
