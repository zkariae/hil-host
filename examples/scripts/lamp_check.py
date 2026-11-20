"""Example script family: the lamp tests L1 and L2 of lamp_plan.yaml on the fake bench, without a board.

The lamp family of examples/hil_host.yaml runs this script from the root of
hil-host with the selected ids:

    python3 examples/scripts/lamp_check.py --only L1 L2

It follows the contract of a script command of the configuration:
  - PASS <id>: <message> or FAIL <id>: <message> gives the verdict of a test;
  - TX  <line> and RX  <line> are the lines sent to and received from the
    firmware;
  - an error starts with Plan error, Serial error or Configuration error
    and goes to stderr;
  - the exit code is 0 when every test passed, 1 when one failed, 2 for a
    bench, plan or configuration error.

The fake bench has no serial port, so the script opens its own session on
the fake firmware of hil_host.fake, which lists the commands of the example
catalogue; a project script opens the port of its bench file instead. Each
test sends params.command with params.args and checks status=OK and every
field of params.expect. An item runs only when its after entries run
before it in the same run.

Not done here: a real bench, and the state of the lamp (the fake firmware
echoes the arguments it receives but keeps no state).

Version: 1.0
Date: 20/11/2026
Author: Z. BEN-AKKA
History:
  1.0  20/11/2026  Initial version, after tests/host/rx_check/uart_rx_check.py 1.20 of stm32f7508-dk-baremetal.
"""

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]  # Root of hil-host: run from root, hil_host is imported without install
sys.path.insert(0, str(ROOT))

from hil_host.bench.link import Link  # noqa: E402
from hil_host.fake.hil1 import FakeFirmware, FakePort, FakeProgrammer  # noqa: E402
from hil_host.protocol.hil1 import Hil1  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_PLAN = HERE / "lamp_plan.yaml"
DEFAULT_CATALOG = HERE.parent / "commands.yaml"
STARTUP = 1.0  # Seconds; the fake answers at once
TIMEOUT = 0.3
EXIT_PASSED, EXIT_FAILED, EXIT_ERROR = 0, 1, 2


class PlanError(Exception):
    """A plan or a selection that cannot run."""


def load_plan(path: Path) -> list:
    """The items of the plan under its key tests; PlanError when unreadable."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PlanError(str(error)) from None
    tests = data.get("tests") if isinstance(data, dict) else None
    if not isinstance(tests, list) or not tests:
        raise PlanError("tests must be a non-empty list")
    for test in tests:
        if not isinstance(test, dict) or "id" not in test or not isinstance(test.get("params"), dict):
            raise PlanError("each test needs an id and params")
    return tests


def select(tests: list, only: list | None) -> list:
    """The selected items in plan order; PlanError for an unknown id or a missing after entry."""
    known = [test["id"] for test in tests]
    unknown = [test_id for test_id in only or [] if test_id not in known]
    if unknown:
        raise PlanError(f"unknown {', '.join(unknown)}")
    chosen = [test for test in tests if only is None or test["id"] in only]
    done = set()
    for test in chosen:
        missing = [before for before in test.get("after", []) if before not in done]
        if missing:
            raise PlanError(f"{test['id']} needs {', '.join(missing)} earlier in the same run")
        done.add(test["id"])
    return chosen


def open_session(catalog: Path) -> Hil1:
    """A ready session on the fake firmware, with its traffic printed as TX and RX lines."""
    names = list(yaml.safe_load(catalog.read_text(encoding="utf-8"))["commands"])
    firmware = FakeFirmware(caps=names)

    def traced(line: str) -> bytes:
        print(f"TX  {line.strip()}")
        answer = firmware.respond(line)
        for received in answer.decode("ascii").splitlines():
            print(f"RX  {received}")
        return answer

    port = FakePort(traced)
    proto = Hil1(TIMEOUT)
    proto.open(Link(port, TIMEOUT, Hil1.max_pending))
    proto.wait_ready(FakeProgrammer(port, firmware).reset, STARTUP, 0.0)
    return proto


def run_test(proto: Hil1, test: dict) -> bool:
    """Send the command of one item, print its verdict; True when it passed."""
    params = test["params"]
    command, args = params["command"], {key: str(value) for key, value in (params.get("args") or {}).items()}
    sent = " ".join([command] + [f"{key}={value}" for key, value in args.items()])
    response = proto.request(command, args)
    if not response.ok:
        print(f"FAIL {test['id']}: {sent}: status={response.status} code={response.code}")
        return False
    for key, expected in (params.get("expect") or {}).items():
        got = response.fields.get(key)
        if got != str(expected):
            print(f"FAIL {test['id']}: {key}: expected {expected}, got {got if got is not None else 'nothing'}")
            return False
    answered = " ".join(f"{key}={response.fields[key]}" for key in params.get("expect") or {})
    print(f"PASS {test['id']}: {sent} answered {answered or 'OK'}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Lamp tests of the example plan, on the fake bench.")
    parser.add_argument("--only", nargs="+", metavar="ID", help="run only these test IDs, e.g. L1 L2")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN, help="lamp test plan file")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help="command catalogue of the fake bench")
    args = parser.parse_args()

    try:
        chosen = select(load_plan(args.plan), args.only)
    except PlanError as error:
        print(f"Plan error in {args.plan}: {error}", file=sys.stderr)
        return EXIT_ERROR
    try:
        proto = open_session(args.catalog)
    except (OSError, yaml.YAMLError, KeyError, TypeError) as error:
        print(f"Configuration error in {args.catalog}: {error}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as error:  # The fake bench did not start: a bench error, not a test verdict
        print(f"Serial error: {error}", file=sys.stderr)
        return EXIT_ERROR

    failed = []
    try:
        for test in chosen:
            if not run_test(proto, test):
                failed.append(test["id"])
    except Exception as error:  # The session is lost: the next tests cannot run
        print(f"Serial error: {error}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        proto.link.close()

    if failed:
        print(f"FAILED: {', '.join(failed)} ({len(chosen) - len(failed)}/{len(chosen)} passed)")
        return EXIT_FAILED
    print(f"ALL PASSED: {len(chosen)}/{len(chosen)}")
    return EXIT_PASSED


if __name__ == "__main__":
    sys.exit(main())
