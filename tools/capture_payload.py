#!/usr/bin/env python3
"""Capture a REDACTED iAqualink robot payload for issue triage.

Reads credentials from a local, gitignored JSON file (default
``.secrets/iaqualink_credentials.json``) and calls the same three
endpoints the integration itself calls, then prints a redacted snapshot
plus a **type report** of the live robot block.

Why this exists
---------------
Issues #88 (EX6000 model ``unknown``), #92 (cleaning modes / add time)
and #100 (CNX stuck Idle) all stalled waiting for a raw payload sample,
each requiring a hand-written Postman walkthrough. The integration's own
diagnostics can't answer these: it dumps ``coordinator.data``, which is
the *parsed* result, not the raw cloud payload.

Redaction is done two ways, and the second is the one that matters:

  1. by KEY   -- any credential-ish key name is masked at any depth.
  2. by VALUE -- the real secret strings (tokens, serial, user id, email,
                 names) are substring-replaced everywhere they appear,
                 including inside nested strings.

(2) is precisely what ``result["debug"] = str(data)`` in ``client.py``
does NOT do: ``async_redact_data`` matches only dict *keys*, so a
stringified payload carries any nested credential straight through to a
user-shared diagnostics file.

The TYPE REPORT is the point. Redacted *values* can't tell you whether
``durations`` is a JSON object or array, or whether ``mode`` is ``1`` or
``"1"`` -- and those two questions are exactly what issue #100 turns on.

Usage
-----
    python tools/capture_payload.py
    python tools/capture_payload.py --creds path/to/other.json

Run it while a cleaning cycle is ACTIVE for the most useful data.
Nothing is written to disk; nothing sensitive is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

try:
    import aiohttp
except ImportError:
    sys.exit("Needs aiohttp:  python -m pip install aiohttp")

URL_LOGIN = "https://prod.zodiac-io.com/users/v1/login"
URL_GET_DEVICES = "https://r-api.iaqualink.net/devices.json"
URL_FEATURES = "https://prod.zodiac-io.com/devices/v2/"
URL_WS = "wss://prod-socket.zodiac-io.com/devices"
API_KEY = "EOOEMOW4YR6QNB07"

DEFAULT_CREDS = Path(".secrets/iaqualink_credentials.json")

ROBOT_TYPES = ("vr", "vortrax", "cyclonext", "cyclobat", "i2d_robot")

# Masked by key name, at any nesting depth.
REDACT_KEYS = {
    "username", "password", "api_key", "apikey", "id_token", "idtoken",
    "auth_token", "authentication_token", "refresh_token", "accesstoken",
    "access_token", "clienttoken", "first_name", "last_name",
    "email", "credentials", "sessionid", "session_id", "appclientid",
    "userpooloauth", "cognitopool", "identityid", "userid",
    "user_id", "serial", "serial_number", "target",
}
_REDACT_KEYS_LOWER = {k.lower() for k in REDACT_KEYS}

# Real secret VALUES, registered at runtime then substring-scrubbed.
_secrets: list[str] = []


def _note_secret(value: Any) -> None:
    """Register a real secret value for substring scrubbing.

    The >=6 length floor avoids turning a short value like a 1-digit id
    into a scrubber that mangles unrelated numbers in the output.
    """
    if isinstance(value, str) and len(value) >= 6:
        _secrets.append(value)


def _scrub_text(text: str) -> str:
    for secret in _secrets:
        if secret and secret in text:
            text = text.replace(secret, "**REDACTED**")
    return text


def redact(obj: Any) -> Any:
    """Recursively redact by key name AND by secret value."""
    if isinstance(obj, dict):
        return {
            key: ("**REDACTED**" if str(key).lower() in _REDACT_KEYS_LOWER
                  else redact(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _scrub_text(obj)
    return obj


def describe_shape(obj: Any, path: str = "") -> list[str]:
    """Report the TYPE of every node.

    This is what settles list-vs-dict and int-vs-string, which redacted
    values alone cannot.
    """
    lines: list[str] = []
    if isinstance(obj, dict):
        lines.append(f"{path or '<root>'}: dict[{len(obj)} keys] keys={list(obj)}")
        for key, value in obj.items():
            lines.extend(describe_shape(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(obj, list):
        lines.append(f"{path}: LIST[{len(obj)}] -> {obj!r}")
    else:
        lines.append(f"{path}: {type(obj).__name__} = {obj!r}")
    return lines


def load_credentials(path: Path) -> list[tuple[str, str, str]]:
    """Return ``[(label, email, password), ...]`` for usable accounts.

    Accepts the multi-account shape::

        {"accounts": [{"label": "vr-main", "email": "...", "password": "..."}]}

    and the legacy single-account shape ``{"email": ..., "password": ...}``.
    Entries with a blank email or password are skipped, so unused slots can
    be left in place rather than deleted.
    """
    if not path.exists():
        sys.exit(
            f"No credentials file at {path}\n"
            'Create it with: {"accounts": [{"label": "a", "email": "...", '
            '"password": "..."}]}\n'
            f"(and make sure it is gitignored)"
        )
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        sys.exit(f"{path} is not valid JSON: {err}")

    raw: list[dict[str, Any]]
    if isinstance(blob.get("accounts"), list):
        raw = [e for e in blob["accounts"] if isinstance(e, dict)]
    elif blob.get("email"):
        raw = [{"label": "account-1", "email": blob.get("email"),
                "password": blob.get("password")}]
    else:
        raw = []

    accounts: list[tuple[str, str, str]] = []
    skipped = 0
    for index, entry in enumerate(raw, start=1):
        label = str(entry.get("label") or f"account-{index}")
        email = str(entry.get("email") or "").strip()
        password = str(entry.get("password") or "")
        if not email or not password or email.startswith("PUT_YOUR"):
            skipped += 1
            continue
        accounts.append((label, email, password))

    if not accounts:
        sys.exit(
            f"No usable accounts in {path} -- every entry has a blank or "
            f"placeholder email/password."
        )
    if skipped:
        print(f"  ({skipped} unfilled slot(s) skipped)")
    return accounts


async def capture_device(
    session: aiohttp.ClientSession,
    dev: dict[str, Any],
    user_id: str,
    id_token: str,
) -> str:
    """Capture one robot. Returns a one-line summary for the roll-up."""
    device_type = dev.get("device_type")
    serial = dev.get("serial_number")
    model = "?"
    durations_desc = "not captured"

    print("\n" + "=" * 72)
    print(f"DEVICE  device_type={device_type}   devices.json id={dev.get('id')!r}")
    print("=" * 72)

    # --- features: the #88 "model unknown" endpoint ------------------------
    try:
        async with session.get(
            f"{URL_FEATURES}{serial}/features",
            headers={"Authorization": id_token, "Accept": "*/*"},
        ) as resp:
            print(f"\n[features] HTTP {resp.status}")
            if resp.status == 200:
                body = await resp.json()
                print(json.dumps(redact(body), indent=2)[:2000])
                print(f"[features] 'model' key present: {'model' in body}")
                # Issue #88: whether this endpoint carries a model at all.
                model = str(body.get("model", "ABSENT"))
    except Exception as err:
        print(f"[features] failed: {type(err).__name__}: {err}")

    if device_type == "i2d_robot":
        print("\n[state] i2d uses the REST hex protocol, not WS -- skipped.")
        return f"{device_type} model={model!r} (i2d: no WS state)"

    # --- live state over WebSocket ----------------------------------------
    frame = {
        "action": "subscribe",
        "namespace": "authorization",
        "payload": {"userId": user_id},
        "service": "Authorization",
        "target": serial,
        "version": 1,
    }
    # The WS handshake is authenticated with the raw IdToken -- no "Bearer "
    # prefix -- exactly as client.py:853-862 does. Omitting these headers
    # yields WSServerHandshakeError 401.
    ws_headers = {"Authorization": id_token, "Accept": "*/*"}
    try:
        async with session.ws_connect(
            URL_WS, headers=ws_headers, heartbeat=30
        ) as ws:
            await ws.send_json(frame)
            for _ in range(8):
                msg = await asyncio.wait_for(ws.receive(), timeout=25)
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                payload = data.get("payload") or {}
                reported = (
                    ((payload.get("robot") or {}).get("state") or {}).get("reported")
                    or (payload.get("state") or {}).get("reported")
                )
                if not reported:
                    continue
                equipment = reported.get("equipment") or {}
                robot = equipment.get("robot") or equipment.get("robot.1")
                if not robot:
                    continue

                which = "robot" if "robot" in equipment else "robot.1"
                print(f"\n[state] equipment key in use: {which!r}")
                print("\n[state] REDACTED robot block:")
                print(json.dumps(redact(robot), indent=2))
                print("\n[state] TYPE REPORT (the important part):")
                for line in describe_shape(robot, "robot"):
                    print("  " + line)

                print("\n" + "-" * 72)
                print("ANSWERS TO THE OPEN QUESTIONS")
                print("-" * 72)
                if "durations" in robot:
                    dur = robot["durations"]
                    print(f"  durations  -> {type(dur).__name__}: {dur!r}")
                    if isinstance(dur, dict):
                        durations_desc = f"dict{list(dur)}"
                    else:
                        durations_desc = f"{type(dur).__name__}[{len(dur)}]"
                else:
                    print("  durations  -> KEY ABSENT")
                    durations_desc = "ABSENT"
                for field in ("mode", "state", "cycle", "prCyc", "stepper",
                              "cycleStartTime", "canister", "totalHours",
                              "totRunTime"):
                    if field in robot:
                        val = robot[field]
                        print(f"  {field:<11}-> {type(val).__name__}: {val!r}")
                return (f"{device_type} model={model!r} equip={which!r} "
                        f"durations={durations_desc}")
            print("[state] no state-bearing frame within 8 messages.")
            return f"{device_type} model={model!r} (no state frame)"
    except Exception as err:
        print(f"[state] WS failed: {type(err).__name__}: {err}")
        return f"{device_type} model={model!r} (WS failed: {type(err).__name__})"


async def capture_account(label: str, email: str, password: str) -> list[str]:
    """Capture every robot on one account. Returns summary lines."""
    print("\n" + "#" * 72)
    print(f"# ACCOUNT: {label}")
    print("#" * 72)

    _note_secret(email)
    _note_secret(password)
    summary: list[str] = []

    async with aiohttp.ClientSession() as session:
        async with session.post(
            URL_LOGIN,
            json={"apiKey": API_KEY, "email": email, "password": password},
            headers={"Content-Type": "application/json; charset=utf-8"},
        ) as resp:
            if resp.status != 200:
                # 401 here is generic -- it covers wrong email, wrong
                # password, and lockout identically.
                print(f"  login FAILED: HTTP {resp.status}")
                return [f"{label}: LOGIN FAILED (HTTP {resp.status})"]
            auth = await resp.json()

        user_id = str(auth.get("id", ""))
        auth_token = auth.get("authentication_token", "")
        id_token = (auth.get("userPoolOAuth") or {}).get("IdToken", "")
        app_client_id = (auth.get("cognitoPool") or {}).get("appClientId", "")
        for value in (user_id, auth_token, id_token, app_client_id,
                      auth.get("first_name"), auth.get("last_name")):
            _note_secret(value)

        print(f"  login OK -- appClientId "
              f"{'present' if app_client_id else 'ABSENT (2-part clientToken)'}")

        async with session.get(
            URL_GET_DEVICES,
            params={"authentication_token": auth_token,
                    "user_id": user_id, "api_key": API_KEY},
        ) as resp:
            devices = await resp.json()

        robots = [d for d in devices if d.get("device_type") in ROBOT_TYPES]
        for dev in robots:
            _note_secret(dev.get("serial_number"))

        print(f"  {len(devices)} device(s), {len(robots)} robot(s): "
              f"{[d.get('device_type') for d in robots]}")
        if not robots:
            return [f"{label}: no robot-type devices"]

        for dev in robots:
            found = await capture_device(session, dev, user_id, id_token)
            summary.append(f"{label}: {found}")

    return summary


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--creds", type=Path, default=DEFAULT_CREDS,
                        help=f"credentials JSON (default: {DEFAULT_CREDS})")
    parser.add_argument("--only", metavar="LABEL",
                        help="capture just the account with this label")
    args = parser.parse_args()

    print("iAqualink payload capture -- output is redacted, nothing written to disk.")
    accounts = load_credentials(args.creds)
    if args.only:
        accounts = [a for a in accounts if a[0] == args.only]
        if not accounts:
            sys.exit(f"No account labelled {args.only!r} in {args.creds}")
    print(f"  {len(accounts)} account(s) to capture: {[a[0] for a in accounts]}")

    summary: list[str] = []
    for label, email, password in accounts:
        try:
            summary.extend(await capture_account(label, email, password))
        except Exception as err:
            print(f"  account {label!r} aborted: {type(err).__name__}: {err}")
            summary.append(f"{label}: ABORTED ({type(err).__name__})")

    print("\n" + "=" * 72)
    print("CROSS-ACCOUNT SUMMARY")
    print("=" * 72)
    for line in summary:
        print("  " + line)

    print("\nDone -- the above is redacted and safe to paste into an issue.")
    print("For richer data, re-run while a cleaning cycle is ACTIVE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
