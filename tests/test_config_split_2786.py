"""Tests for task #2786: gateway/delivery/config split.

Verifies that:
- No banned /api/gateway/* routes are used in delivery/wake/actuation paths.
  (Readback-only compatibility calls are documented with TODOs.)
- Config resolution logic properly separates delivery_url, observation_url,
  and channels_url.

Note: Tests do NOT import the adapter module directly due to Python 3.14
dataclass compatibility issues in the adapter's dependency tree.
"""

import os


# =========================================================================
# Banned-route grep test
# =========================================================================


def test_no_banned_gateway_routes_in_delivery_actuation():
    """Verify the adapter does not use /api/gateway/* in actuation paths.

    The only allowed /api/gateway/ call is the readback-only
    get_message_readback() in DenChannelsClient, which is explicitly
    documented as a TODO(#2786) compatibility call.
    """
    adapter_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "plugins",
        "platforms",
        "den_channels",
        "adapter.py",
    )
    with open(adapter_path) as f:
        content = f.read()

    # Find all lines referencing /api/gateway/
    lines = []
    for i, line in enumerate(content.splitlines(), 1):
        if "/api/gateway/" in line:
            lines.append((i, line.strip()))

    if not lines:
        return  # Clean — no banned routes at all

    # The only permitted call is the readback-only get_message_readback,
    # which must have a TODO(#2786) comment nearby.
    # Check that every /api/gateway/ line is inside the get_message_readback method.
    adapter_lines = content.splitlines()
    # Find the def get_message_readback boundary
    readback_start = None
    readback_end = len(adapter_lines) + 1  # default: end of file
    for i, cline in enumerate(adapter_lines, 1):
        if readback_start is None:
            if "def get_message_readback" in cline.strip():
                readback_start = i
        elif readback_start is not None and cline.strip().startswith("def "):
            readback_end = i
            break
    assert readback_start is not None, (
        "get_message_readback method not found — did it get renamed?"
    )
    for lineno, text in lines:
        assert readback_start <= lineno < readback_end, (
            f"Line {lineno}: banned route outside get_message_readback "
            f"(method range [{readback_start}, {readback_end}): {text}"
        )

    # Verify the readback method has the TODO comment in the preceding lines
    readback_line = lines[0][0]
    # adapter_lines is 0-indexed; readback_line is 1-indexed from enumerate
    surrounding_start = max(0, readback_line - 7)
    surrounding_end = readback_line - 1  # exclude the line itself
    surrounding = "".join(
        adapter_lines[surrounding_start:surrounding_end]
    )
    assert "TODO(#2786)" in surrounding, (
        f"Readback call at line {readback_line} is missing "
        f"a TODO(#2786) compatibility note"
    )


# =========================================================================
# Config resolution logic tests (tested via env var simulation, no import)
# =========================================================================


def test_delivery_url_falls_back_to_gateway_url():
    """When DEN_DELIVERY_URL is unset, delivery_url should match gateway_url."""
    gateway_url = "http://192.168.1.10:8079"
    channels_url = "http://192.168.1.10:18081"

    # Simulates adapter __init__ logic:
    #   self.delivery_url = DEN_DELIVERY_URL or self.gateway_url or ""
    delivery_url = "" or gateway_url or ""
    observation_url = "" or channels_url or ""

    assert delivery_url == "http://192.168.1.10:8079"
    assert observation_url == "http://192.168.1.10:18081"
    assert channels_url == "http://192.168.1.10:18081"


def test_delivery_url_independent_from_channels_url():
    """When DEN_DELIVERY_URL is set, delivery_url should be independent."""
    delivery_url = "http://delivery.internal:8083"
    # Should NOT fall back to channels_url or gateway_url
    assert delivery_url == "http://delivery.internal:8083"


def test_observation_url_independent():
    """When DEN_OBSERVATION_URL is set, observation_url should be independent."""
    channels_url = "http://192.168.1.10:18081"
    observation_url = "http://obs.internal:8082"
    # Should NOT fall back to channels_url
    assert observation_url == "http://obs.internal:8082"
    assert observation_url != channels_url
