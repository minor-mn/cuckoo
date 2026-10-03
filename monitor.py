#!/usr/bin/env python3
"""Post a configured daily message and follow new followers.

The script is intentionally dependency-free so it can run directly from cron.
It uses the X API v2 with OAuth 1.0a credentials belonging to the target account.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import tempfile
import time
import tomllib
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


LOG = logging.getLogger("x-post-monitor")
DEFAULT_POST_TEXT = "yyyy.mm.dd 阪神戦まとめ"


class XApiError(RuntimeError):
    """An actionable X API error."""


def oauth_percent_encode(value: str | int) -> str:
    """Encode a value according to RFC 5849's OAuth encoding rules."""
    return quote(str(value), safe="~-._")


def oauth1_authorization_header(
    method: str,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    access_token_secret: str,
    timestamp: str,
    nonce: str,
) -> str:
    """Create an OAuth 1.0a Authorization header for an HTTP request."""
    oauth_parameters = [
        ("oauth_consumer_key", consumer_key),
        ("oauth_nonce", nonce),
        ("oauth_signature_method", "HMAC-SHA1"),
        ("oauth_timestamp", timestamp),
        ("oauth_token", access_token),
        ("oauth_version", "1.0"),
    ]

    parsed_url = urlsplit(url)
    request_parameters = parse_qsl(parsed_url.query, keep_blank_values=True)
    signature_parameters = request_parameters + oauth_parameters
    normalized_parameters = "&".join(
        f"{oauth_percent_encode(key)}={oauth_percent_encode(value)}"
        for key, value in sorted(
            signature_parameters,
            key=lambda parameter: (
                oauth_percent_encode(parameter[0]),
                oauth_percent_encode(parameter[1]),
            ),
        )
    )
    base_url = urlunsplit(
        (parsed_url.scheme, parsed_url.netloc, parsed_url.path or "/", "", "")
    )
    signature_base_string = "&".join(
        (
            method.upper(),
            oauth_percent_encode(base_url),
            oauth_percent_encode(normalized_parameters),
        )
    )
    signing_key = "&".join(
        (
            oauth_percent_encode(consumer_secret),
            oauth_percent_encode(access_token_secret),
        )
    )
    signature = base64.b64encode(
        hmac.new(
            signing_key.encode("utf-8"),
            signature_base_string.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")
    oauth_parameters.append(("oauth_signature", signature))

    return "OAuth " + ", ".join(
        f'{oauth_percent_encode(key)}="{oauth_percent_encode(value)}"'
        for key, value in oauth_parameters
    )


class XClient:
    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        access_token: str,
        access_token_secret: str,
        base_url: str = "https://api.x.com",
        bearer_token: str = "",
    ) -> None:
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
        bearer: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        if bearer:
            if not self.bearer_token:
                raise XApiError("A bearer token is required for this X API lookup")
            authorization = f"Bearer {self.bearer_token}"
        else:
            authorization = oauth1_authorization_header(
                method,
                url,
                consumer_key=self.consumer_key,
                consumer_secret=self.consumer_secret,
                access_token=self.access_token,
                access_token_secret=self.access_token_secret,
                timestamp=str(int(time.time())),
                nonce=secrets.token_hex(16),
            )
        headers = {
            "Authorization": authorization,
            "Accept": "application/json",
            "User-Agent": "hanshin-post-monitor/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                status = response.status
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise XApiError(f"X API returned HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise XApiError(f"X API request failed: {error.reason}") from error

        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as error:
            raise XApiError(f"X API returned invalid JSON (HTTP {status})") from error

        if status < 200 or status >= 300:
            raise XApiError(f"X API returned HTTP {status}: {payload}")
        if payload.get("errors"):
            raise XApiError(f"X API returned errors: {payload['errors']}")
        return payload

    def get_authenticated_user(self) -> dict[str, Any]:
        response = self._request("GET", "/2/users/me", params={"user.fields": "id,username"})
        try:
            return response["data"]
        except KeyError as error:
            raise XApiError(f"X API did not return the authenticated user: {response}") from error

    def get_followers_page(
        self, user_id: str, pagination_token: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "max_results": 1000,
            "user.fields": "id,username",
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        return self._request(
            "GET", f"/2/users/{user_id}/followers", params=params, bearer=True
        )

    def follow_user(self, source_user_id: str, target_user_id: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/2/users/{source_user_id}/following",
            body={"target_user_id": target_user_id},
        )
        return response.get("data", {})

    def create_post(self, text: str) -> dict[str, Any]:
        response = self._request("POST", "/2/tweets", body={"text": text})
        try:
            return response["data"]
        except KeyError as error:
            raise XApiError(f"X API did not return the created post: {response}") from error


def render_post_text(template: str, local_date: date) -> str:
    """Render the supported date tokens in the configured post template."""
    return (
        template.replace("yyyy", f"{local_date.year:04d}")
        .replace("mm", f"{local_date.month:02d}")
        .replace("dd", f"{local_date.day:02d}")
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read state file {path}: {error}") from error
    if not isinstance(state, dict):
        raise RuntimeError(f"State file {path} must contain a JSON object")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent, text=True
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, ensure_ascii=False, indent=2)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def follow_new_followers(
    client: XClient,
    *,
    target_user_id: str,
    today_string: str,
    followers_path: Path,
) -> None:
    follower_state = load_state(followers_path)
    if follower_state.get("date") == today_string:
        return

    saved_ids = follower_state.get("user_ids")
    if saved_ids is None:
        # Establish a baseline without following everybody already present.
        all_ids: list[str] = []
        pagination_token: str | None = None
        while True:
            response = client.get_followers_page(target_user_id, pagination_token)
            all_ids.extend(str(user["id"]) for user in response.get("data", []))
            pagination_token = response.get("meta", {}).get("next_token")
            if not pagination_token:
                break
        save_state(followers_path, {"date": today_string, "user_ids": all_ids})
        LOG.info("Saved initial follower baseline: %d users", len(all_ids))
        return

    if not isinstance(saved_ids, list) or not all(isinstance(user_id, str) for user_id in saved_ids):
        raise RuntimeError(f"Follower state file {followers_path} has an invalid user_ids value")

    known_ids = set(saved_ids)
    new_ids: list[str] = []
    pagination_token = None
    found_known_id = False
    while not found_known_id:
        response = client.get_followers_page(target_user_id, pagination_token)
        for user in response.get("data", []):
            user_id = str(user["id"])
            if user_id in known_ids:
                found_known_id = True
                break
            new_ids.append(user_id)
            result = client.follow_user(target_user_id, user_id)
            LOG.info("Followed new follower %s (following=%s)", user_id, result.get("following"))
        if found_known_id:
            break
        pagination_token = response.get("meta", {}).get("next_token")
        if not pagination_token:
            break

    save_state(
        followers_path,
        {"date": today_string, "user_ids": new_ids + saved_ids},
    )
    if new_ids:
        LOG.info("Followed %d new followers", len(new_ids))


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(
            f"Missing config file: {path}. Copy config.toml.example to config.toml and fill it in."
        )
    try:
        with path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RuntimeError(f"Could not read config file {path}: {error}") from error
    if not isinstance(config, dict):
        raise RuntimeError(f"Config file {path} must contain a TOML table")
    return config


def config_section(config: dict[str, Any], name: str) -> dict[str, Any]:
    section = config.get(name, {})
    if not isinstance(section, dict):
        raise RuntimeError(f"Config section must be a table: {name}")
    return section


def required_config(config: dict[str, Any], name: str) -> str:
    value = config.get(name, "")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Required config value is missing: {name}")
    return value.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Path to the TOML configuration file (default: config.toml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Find and print the candidate post without creating a new post",
    )
    return parser.parse_args()


def run(config_path: Path, dry_run: bool = False) -> int:
    config = load_config(config_path)
    auth_config = config_section(config, "auth")
    target_config = config_section(config, "target")
    system_config = config_section(config, "system")

    consumer_key = required_config(auth_config, "consumer_key")
    consumer_secret = required_config(auth_config, "consumer_secret")
    access_token = required_config(auth_config, "access_token")
    access_token_secret = required_config(auth_config, "access_token_secret")
    bearer_token = str(auth_config.get("bearer_token", "")).strip()
    target_username = str(target_config.get("username", "tigerslivecom")).strip().lstrip("@")
    post_text_template = target_config.get("post_text", DEFAULT_POST_TEXT)
    if not isinstance(post_text_template, str) or not post_text_template:
        raise RuntimeError("Config value must be a non-empty string: target.post_text")
    state_path = Path(str(system_config.get("state_file", "state.json")))
    followers_path = Path(str(system_config.get("followers_file", "followers.json")))
    api_base_url = str(system_config.get("api_base_url", "https://api.x.com"))
    timezone_name = str(system_config.get("timezone", "Asia/Tokyo"))

    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise RuntimeError(f"Unknown timezone: {timezone_name}") from error

    lock_path = state_path.with_name(f".{state_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("Another run is already in progress; exiting")
            return 0

        state = load_state(state_path)
        follower_state = load_state(followers_path)
        now = datetime.now(tz)
        today = now.date()
        today_string = today.isoformat()
        is_dry_run = dry_run or bool(system_config.get("dry_run", False))
        should_check_followers = follower_state.get("date") != today_string

        if state.get("date") == today_string and state.get("target_post_id"):
            already_posted = True
        else:
            already_posted = False

        if already_posted:
            LOG.info("Already posted today's message; target=@%s", target_username)

        client: XClient | None = None
        if should_check_followers and not is_dry_run:
            client = XClient(
                consumer_key,
                consumer_secret,
                access_token,
                access_token_secret,
                api_base_url,
                bearer_token,
            )
            authenticated_user = client.get_authenticated_user()
            target_user_id = str(authenticated_user["id"])
            follow_new_followers(
                client,
                target_user_id=target_user_id,
                today_string=today_string,
                followers_path=followers_path,
            )

        if now.hour != 13:
            if not already_posted:
                LOG.info("Outside posting window (13:00-13:59); skipping daily post")
            return 0

        if already_posted:
            return 0

        if client is None:
            client = XClient(
                consumer_key,
                consumer_secret,
                access_token,
                access_token_secret,
                api_base_url,
                bearer_token,
            )
        post_text = render_post_text(post_text_template, today)
        LOG.info("Post text: %s", post_text)
        if is_dry_run:
            LOG.info("Dry run; no post created")
            return 0

        created = client.create_post(post_text)
        target_post_id = str(created["id"])
        save_state(
            state_path,
            {
                "date": today_string,
                "target_post_id": target_post_id,
                "posted_text": post_text,
            },
        )
        LOG.info("Posted daily message to @%s as %s", target_username, target_post_id)
        return 0


def main() -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        args = parse_args()
        return run(args.config, args.dry_run)
    except (RuntimeError, XApiError) as error:
        LOG.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
