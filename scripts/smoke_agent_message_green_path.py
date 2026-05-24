#!/usr/bin/env python3
"""Smoke/preflight Den Channels green-path direct-agent messaging.

Default mode is non-mutating: resolve channel, preflight active agent membership,
and print the evidence that would be used for a direct-agent wake. Pass --send to
actually POST /api/gateway/direct-agent-messages.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from typing import Any

AGENT_COMMONS_CHANNEL_ID = 21
DEFAULT_BASE_URL = "http://192.168.1.10:18080"


def request_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310 - operator-provided LAN URL.
        return json.loads(response.read().decode("utf-8"))


def get_memberships(base_url: str, *, project_id: str | None, channel_id: int | None) -> dict[str, Any]:
    if channel_id is not None:
        query = urllib.parse.urlencode({"channelId": channel_id})
    elif project_id:
        query = urllib.parse.urlencode({"projectId": project_id})
    else:
        query = urllib.parse.urlencode({"channelId": AGENT_COMMONS_CHANNEL_ID})
    return request_json("GET", f"{base_url}/api/gateway/memberships?{query}")


def active_member(memberships: dict[str, Any], member_identity: str) -> dict[str, Any] | None:
    for member in memberships.get("members") or []:
        if (
            member.get("memberIdentity") == member_identity
            and member.get("memberType") == "agent"
            and member.get("membershipStatus") == "active"
        ):
            return member
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("member_identity")
    parser.add_argument("body", nargs="?", default="Den Channels green-path wake smoke")
    parser.add_argument("--project-id")
    parser.add_argument("--channel-id", type=int)
    parser.add_argument("--sender-identity", default="den-hermes-bridge-smoke")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--send", action="store_true", help="actually send the direct-agent message")
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    memberships = get_memberships(base_url, project_id=args.project_id, channel_id=args.channel_id)
    channel_id = memberships.get("channelId")
    channel_slug = memberships.get("channelSlug")
    member = active_member(memberships, args.member_identity)
    result: dict[str, Any] = {
        "status": "preflight_ok" if member else "not_sent",
        "memberIdentity": args.member_identity,
        "channelId": channel_id,
        "channelSlug": channel_slug,
        "channelKind": memberships.get("channelKind"),
        "projectId": memberships.get("projectId"),
        "membershipStatus": member.get("membershipStatus") if member else None,
        "wakePolicy": member.get("wakePolicy") if member else None,
        "gatewayEventsUrl": f"{base_url}/api/gateway/events?{urllib.parse.urlencode({'channelId': channel_id, 'afterId': 0})}",
    }
    if not member:
        result["diagnostic"] = f"{args.member_identity} is not an active agent member of channel {channel_id} ({channel_slug})"
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2
    if args.send:
        payload = {
            "channelId": channel_id,
            "memberIdentity": args.member_identity,
            "body": args.body,
            "senderIdentity": args.sender_identity,
        }
        response = request_json("POST", f"{base_url}/api/gateway/direct-agent-messages", payload)
        message_id = response.get("messageId") or (response.get("message") or {}).get("id") or response.get("id")
        result.update(
            {
                "status": "sent",
                "messageId": message_id,
                "sendResponse": response,
                "gatewayMessageUrl": f"{base_url}/api/gateway/messages/{message_id}" if message_id else None,
            }
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
