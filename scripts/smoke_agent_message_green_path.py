#!/usr/bin/env python3
"""Smoke/preflight successor Delivery + Conversation direct-agent messaging.

Default mode is non-mutating: resolve Conversation successor membership,
preflight active agent membership, and print the evidence that would be used for
a Delivery successor intent. Pass --send to create a Delivery intent.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from typing import Any

AGENT_COMMONS_CHANNEL_ID = 21
DEFAULT_BASE_URL = "http://192.168.1.10:8079"


def request_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310 - operator-provided LAN URL.
        return json.loads(response.read().decode("utf-8"))


def get_memberships(base_url: str, *, project_id: str | None, channel_id: int | None) -> dict[str, Any]:
    if channel_id is not None:
        query = urllib.parse.urlencode({"channel_id": channel_id})
    elif project_id:
        query = urllib.parse.urlencode({"project_id": project_id})
    else:
        query = urllib.parse.urlencode({"channel_id": AGENT_COMMONS_CHANNEL_ID})
    return request_json("GET", f"{base_url}/v1/conversation/memberships?{query}")


def active_member(memberships: dict[str, Any], member_identity: str) -> dict[str, Any] | None:
    for member in memberships.get("members") or []:
        if (
            (member.get("memberIdentity") or member.get("member_identity")) == member_identity
            and (member.get("memberType") or member.get("member_type")) == "agent"
            and (member.get("membershipStatus") or member.get("membership_status")) == "active"
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
    rows = memberships.get("memberships") or memberships.get("items") or []
    first = rows[0] if rows else {}
    channel_id = memberships.get("channelId") or memberships.get("channel_id") or first.get("channelId") or first.get("channel_id")
    channel_slug = memberships.get("channelSlug") or memberships.get("channel_slug") or first.get("channelSlug") or first.get("channel_slug")
    member = active_member(memberships, args.member_identity)
    result: dict[str, Any] = {
        "status": "preflight_ok" if member else "not_sent",
        "memberIdentity": args.member_identity,
        "channelId": channel_id,
        "channelSlug": channel_slug,
        "channelKind": memberships.get("channelKind") or memberships.get("channel_kind") or first.get("channelKind") or first.get("channel_kind"),
        "projectId": memberships.get("projectId") or memberships.get("project_id") or first.get("projectId") or first.get("project_id"),
        "membershipStatus": (member.get("membershipStatus") or member.get("membership_status")) if member else None,
        "wakePolicy": (member.get("wakePolicy") or member.get("wake_policy")) if member else None,
        "deliveryIntentsUrl": f"{base_url}/v1/delivery/intents?{urllib.parse.urlencode({'channelId': channel_id, 'afterId': 0})}",
    }
    if not member:
        result["diagnostic"] = f"{args.member_identity} is not an active agent member of channel {channel_id} ({channel_slug})"
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2
    if args.send:
        payload = {
            "target_identity": {"profile": args.member_identity, "instance_id": args.member_identity},
            "idempotency_key": f"smoke:{channel_id}:{args.member_identity}",
            "source_ref": f"wake://{args.member_identity}?body={urllib.parse.quote(args.body)}",
            "channel_id": channel_id,
            "ttl_seconds": 300,
        }
        response = request_json("POST", f"{base_url}/v1/delivery/intents", payload)
        message_id = response.get("messageId") or (response.get("message") or {}).get("id") or response.get("id")
        result.update(
            {
                "status": "sent",
                "messageId": message_id,
                "sendResponse": response,
                "deliveryIntentUrl": f"{base_url}/v1/delivery/intents/{message_id}" if message_id else None,
            }
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
