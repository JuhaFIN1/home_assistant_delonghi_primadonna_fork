"""A timeout should say which command failed."""

import asyncio
import os
import sys

sys.path.append(
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "custom_components",
        )
    )
)

from delonghi_primadonna.const import BYTES_POWER  # noqa: E402
from delonghi_primadonna.device import describe_command  # noqa: E402


async def test_known_commands_are_named():
    assert describe_command(BYTES_POWER) == "power"
    assert describe_command([0x0D, 0x07, 0xA4]) == "profile names"
    assert describe_command([0x0D, 0x07, 0x95]) == "read setting"
    assert describe_command([0x0D, 0x06, 0xA9]) == "profile selection"


async def test_every_command_the_code_sends_has_a_name():
    """A command type that is sent but unnamed defeats the purpose."""
    import re

    root = os.path.join(
        os.path.dirname(__file__), "..", "custom_components",
        "delonghi_primadonna",
    )
    with open(os.path.join(root, "const.py"), encoding="utf-8") as handle:
        const = handle.read()
    with open(os.path.join(root, "device.py"), encoding="utf-8") as handle:
        device = handle.read()

    sent = set()
    for match in re.finditer(r"^BYTES_[A-Z0-9_]+ = \[([^\]]+)\]", const, re.M):
        parts = [p.strip() for p in match.group(1).split(",")]
        if len(parts) > 2:
            sent.add(int(parts[2], 16))
    for byte in re.findall(
        r"\[0x0[dD],\s*0x[0-9a-fA-F]{2},\s*0x([0-9a-fA-F]{2})", device
    ):
        sent.add(int(byte, 16))

    unnamed = sorted(
        hex(op) for op in sent if describe_command([0, 0, op]).startswith("0x")
    )
    assert not unnamed, f"sent but unnamed: {unnamed}"


async def test_unknown_command_falls_back_to_its_type_byte():
    assert describe_command([0x0D, 0x07, 0x5A]) == "0x5a"


async def test_short_message_does_not_raise():
    assert describe_command([0x0D]) == "unknown"
    assert describe_command([]) == "unknown"


async def run_tests():
    await test_known_commands_are_named()
    await test_every_command_the_code_sends_has_a_name()
    await test_unknown_command_falls_back_to_its_type_byte()
    await test_short_message_does_not_raise()
    print("[SUCCESS] Command naming verified.")


if __name__ == "__main__":
    asyncio.run(run_tests())
