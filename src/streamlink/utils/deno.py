"""Deno-backed JavaScript challenge solvers for the YouTube plugin.

:class:`DenoJCP` spawns a sandboxed ``deno run`` subprocess, feeds it the
YouTube player JS bundle together with the bundled ``lib`` / ``core`` solvers
scripts, and parses the JSON result to produce a solved
:class:`NChallengeOutput`.
"""
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys

from streamlink.utils.path import resolve_executable

log = logging.getLogger(__name__)


class Deno:
    """Solves YouTube n-parameter challenges by executing JS inside Deno."""
    def __init__(self):
        self._exec_path = resolve_executable('deno')

    def execute(self, stdin) -> str:
        # TODO adding/editing parameters
        cmd = [
            self._exec_path, "run",
            "--ext=js",
            "--no-code-cache",
            "--no-prompt",
            "--no-remote",
            "--no-lock",
            "--node-modules-dir=none",
            "--no-config",
            "--no-npm",
            "--cached-only",
            "-",
        ]
        log.debug("Executing Deno: %s", shlex.join(cmd))

        proc = subprocess.Popen(
            cmd,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
            encoding="utf-8",
        )
        try:
            stdout, stderr = proc.communicate(stdin)
        except BaseException:
            proc.kill()
            proc.wait(timeout=0)
            raise

        if proc.returncode or stderr:
            msg = f"Deno process failed (returncode: {proc.returncode})"
            if stderr:
                msg = f"{msg}: {stderr.strip()}"
            raise Exception(msg)

        log.debug("Deno process completed successfully")
        return stdout
