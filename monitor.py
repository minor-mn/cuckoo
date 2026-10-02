#!/usr/bin/env python3
"""Copy the first matching Hanshin Tigers post of each day.

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
from datetime import date, datetime, time as datetime_time, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


LOG = logging.getLogger("x-post-monitor")
DEFAULT_KEYWORD_1 = "【 一軍 】"
DEFAULT_KEYWORD_2 = "【 阪神 】"
DEFAULT_POST_TEXT = "yyyy.mm.dd 阪神戦まとめ"


class XApiError(RuntimeError):
    """An actionable X API error."""


def oauth_percent_encode(value: str | int) -> str:
    """Encode a value according to RFC 5849's OAuth encoding rules."""
    return quote(str(value), safe="~-._")


def format_api_datetime(value: datetime) -> str:
    """Format an aware datetime as an RFC 3339 UTC timestamp."""
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


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
    ) -> None:
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret
        self.base_url = base_url.rstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": oauth1_authorization_header(
                method,
                url,
                consumer_key=self.consumer_key,
                consumer_secret=self.consumer_secret,
                access_token=self.access_token,
                access_token_secret=self.access_token_secret,
                timestamp=str(int(time.time())),
                nonce=secrets.token_hex(16),
            ),
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

    def get_user(self, username: str) -> dict[str, Any]:
        encoded_username = quote(username.lstrip("@"), safe="")
        response = self._request(
            "GET",
            f"/2/users/by/username/{encoded_username}",
            params={"user.fields": "id,username"},
        )
        try:
            return response["data"]
        except KeyError as error:
            raise XApiError(f"X API did not return a user for @{username}") from error

    def get_timeline_page(
        self,
        user_id: str,
        pagination_token: str | None = None,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        since_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "max_results": 5,
            "exclude": "retweets,replies",
            "tweet.fields": "created_at,text",
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        if start_time:
            params["start_time"] = format_api_datetime(start_time)
        if end_time:
            params["end_time"] = format_api_datetime(end_time)
        if since_id:
            params["since_id"] = since_id
        return self._request("GET", f"/2/users/{user_id}/tweets", params=params)

    def get_posts_for_local_date(
        self,
        user_id: str,
        local_today: date,
        tz: ZoneInfo,
        since_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch only today's posts, five posts per page."""
        posts: list[dict[str, Any]] = []
        pagination_token: str | None = None
        newest_id: str | None = None
        start_time = datetime.combine(local_today, datetime_time.min, tzinfo=tz)
        end_time = datetime.now(tz)

        while True:
            response = self.get_timeline_page(
                user_id,
                pagination_token,
                start_time=start_time,
                end_time=end_time,
                since_id=since_id,
            )
            page = response.get("data", [])
            posts.extend(page)
            if not page:
                break

            metadata = response.get("meta", {})
            if newest_id is None:
                newest_id_value = metadata.get("newest_id")
                if newest_id_value:
                    newest_id = str(newest_id_value)

            pagination_token = metadata.get("next_token")
            if not pagination_token:
                break

        return posts, newest_id

    def create_post(self, text: str) -> dict[str, Any]:
        response = self._request("POST", "/2/tweets", body={"text": text})
        try:
            return response["data"]
        except KeyError as error:
            raise XApiError(f"X API did not return the created post: {response}") from error


def parse_created_at(value: str) -> datetime:
    """Parse the RFC 3339 timestamp returned by the X API."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def render_post_text(template: str, local_date: date) -> str:
    """Render the supported date tokens in the configured post template."""
    return (
        template.replace("yyyy", f"{local_date.year:04d}")
        .replace("mm", f"{local_date.month:02d}")
        .replace("dd", f"{local_date.day:02d}")
    )


def find_first_matching_post(
    posts: list[dict[str, Any]],
    *,
    local_today: date,
    tz: ZoneInfo,
    keyword_1: str,
    keyword_2: str,
) -> dict[str, Any] | None:
    matching: list[dict[str, Any]] = []
    for post in posts:
        text = post.get("text", "")
        created_at_value = post.get("created_at")
        if not created_at_value or keyword_1 not in text or keyword_2 not in text:
            continue
        created_at = parse_created_at(created_at_value)
        if created_at.astimezone(tz).date() == local_today:
            matching.append(post)

    return min(matching, key=lambda post: parse_created_at(post["created_at"])) if matching else None


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
    source_config = config_section(config, "source")
    target_config = config_section(config, "target")
    matching_config = config_section(config, "matching")
    system_config = config_section(config, "system")

    consumer_key = required_config(auth_config, "consumer_key")
    consumer_secret = required_config(auth_config, "consumer_secret")
    access_token = required_config(auth_config, "access_token")
    access_token_secret = required_config(auth_config, "access_token_secret")
    source_username = str(source_config.get("username", "hanshintigersjp")).strip()
    target_username = str(target_config.get("username", "tigerslivecom")).strip().lstrip("@")
    post_text_template = target_config.get("post_text", DEFAULT_POST_TEXT)
    if not isinstance(post_text_template, str) or not post_text_template:
        raise RuntimeError("Config value must be a non-empty string: target.post_text")
    keyword_1 = str(matching_config.get("keyword_1", DEFAULT_KEYWORD_1))
    keyword_2 = str(matching_config.get("keyword_2", DEFAULT_KEYWORD_2))
    state_path = Path(str(system_config.get("state_file", "state.json")))
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
        now = datetime.now(tz)
        today = now.date()
        today_string = today.isoformat()

        if state.get("date") == today_string and state.get("source_post_id"):
            LOG.info(
                "Already posted today's summary for source post %s; target=%s",
                state["source_post_id"],
                target_username,
            )
            return 0

        is_dry_run = dry_run or bool(system_config.get("dry_run", False))
        since_id = None
        if state.get("date") == today_string:
            saved_since_id = state.get("last_seen_source_post_id")
            if saved_since_id:
                since_id = str(saved_since_id)

        client = XClient(
            consumer_key,
            consumer_secret,
            access_token,
            access_token_secret,
            api_base_url,
        )
        source_user_id = str(source_config.get("user_id", "")).strip()
        if not source_user_id:
            source_user = client.get_user(source_username)
            source_user_id = str(source_user["id"])
            LOG.info("Monitoring @%s (user id %s)", source_user["username"], source_user_id)

        posts, newest_source_post_id = client.get_posts_for_local_date(
            source_user_id,
            today,
            tz,
            since_id=since_id,
        )
        candidate = find_first_matching_post(
            posts,
            local_today=today,
            tz=tz,
            keyword_1=keyword_1,
            keyword_2=keyword_2,
        )
        if candidate is None:
            LOG.info("No matching post for %s", today_string)
            if newest_source_post_id and not is_dry_run:
                save_state(
                    state_path,
                    {
                        "date": today_string,
                        "last_seen_source_post_id": newest_source_post_id,
                    },
                )
            return 0

        candidate_id = str(candidate["id"])
        candidate_text = candidate.get("text", "")
        candidate_created_at = candidate["created_at"]
        if not candidate_text:
            LOG.warning("Candidate %s has no text; skipping", candidate_id)
            return 0

        LOG.info(
            "Found first matching post %s at %s: %s",
            candidate_id,
            candidate_created_at,
            candidate_text,
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
                "source_post_id": candidate_id,
                "source_created_at": candidate_created_at,
                "target_post_id": target_post_id,
                "posted_text": post_text,
            },
        )
        LOG.info("Posted summary for source post %s to @%s as %s", candidate_id, target_username, target_post_id)
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
