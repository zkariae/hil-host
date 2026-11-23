"""Flash and reset the board with the commands of the project configuration.

The commands stay in the project: this module only runs image.flash,
image.reset and image.reset_sw of the configuration (hil_host/config.py),
from root, each within image.timeout. A command that fails, hangs or cannot
start raises ProgrammingFailure. On the hil.programmer logger, each command
is logged at INFO as RUN flash:, RUN reset: or RUN reset_sw: and the command
(shown live with pytest --log-cli-level=INFO), and its output at DEBUG (log
file only). build_id() reads the build of the image with image.build_id, the
build the readiness signal reports.

Not done here: after a timeout only the command itself is killed, not the
processes it started.

Version: 1.3
Date: 23/11/2026
Author: Z. BEN-AKKA
History:
  1.0  26/10/2026  Initial version.
  1.1  26/10/2026  Log each make command at INFO for live display.
  1.2  26/10/2026  Add the software reset (make reset-sw).
  1.3  23/11/2026  Moved from stm32f7508-dk-baremetal tests/host/hil/adapters/programmer.py, version 1.2; commands, timeout and build source come from the configuration.
"""

import logging
import shlex
import subprocess
from pathlib import Path

from hil_host import LOGGER
from hil_host.bench.outcomes import ProgrammingFailure
from hil_host.config import Image, expand

OUTPUT_TAIL_LINES = 15  # Output lines kept in a failure message

log = logging.getLogger(f"{LOGGER}.programmer")


class Programmer:
    """The commands of one bench image, run from the root of the project."""

    def __init__(self, image: Image, root: Path | str):
        self.image = image
        self.root = Path(root)

    def flash(self) -> None:
        """Build the image if needed and program it; the board then boots it."""
        self._run("flash", self.image.flash)

    def reset(self) -> None:
        """Reset the board without reprogramming it."""
        self._run("reset", self.image.reset)

    def reset_sw(self) -> None:
        """Software reset: the readiness signal then reports it as its reset cause."""
        self._run("reset_sw", self.image.reset_sw)

    def build_id(self) -> str | None:
        """The build of the last built image, or None without a build source, a file or a match."""
        image = self.image
        if image.build_file is None or not image.build_file.is_file():
            return None
        match = image.build_pattern.search(image.build_file.read_text(encoding="utf-8", errors="replace"))
        return match.group(1) if match else None

    def _run(self, action: str, argv: tuple) -> None:
        command = expand(argv)
        log.info("RUN %s: %s", action, shlex.join(command))
        try:
            # Any byte is accepted: an undecodable output must not hide the outcome of the command
            done = subprocess.run(command, cwd=self.root, capture_output=True, encoding="utf-8", errors="replace",
                                  timeout=self.image.timeout, check=False)
        except subprocess.TimeoutExpired:
            raise ProgrammingFailure(f"{action} did not finish within {self.image.timeout} s") from None
        except OSError as error:
            raise ProgrammingFailure(f"{action} could not start: {error}") from None
        output = (done.stdout + done.stderr).strip()
        for line in output.splitlines():
            log.debug("%s: %s", action, line)
        if done.returncode != 0:
            tail = "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])
            raise ProgrammingFailure(f"{action} failed (exit {done.returncode}):\n{tail}")
