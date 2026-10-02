import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from monitor import (
    XClient,
    find_first_matching_post,
    load_config,
    oauth1_authorization_header,
    render_post_text,
)


class FindFirstMatchingPostTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Asia/Tokyo")
        self.today = date(2026, 10, 2)

    def test_returns_earliest_matching_post_on_local_date(self) -> None:
        posts = [
            {
                "id": "later",
                "created_at": "2026-10-02T03:00:00.000Z",
                "text": "【 一軍 】 【 阪神 】 later",
            },
            {
                "id": "first",
                "created_at": "2026-10-01T15:10:00.000Z",
                "text": "【 一軍 】 【 阪神 】 first",
            },
            {
                "id": "other",
                "created_at": "2026-10-02T02:00:00.000Z",
                "text": "【 一軍 】 only",
            },
        ]

        result = find_first_matching_post(
            posts,
            local_today=self.today,
            tz=self.tz,
            keyword_1="【 一軍 】",
            keyword_2="【 阪神 】",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "first")

    def test_ignores_posts_from_previous_local_date(self) -> None:
        posts = [
            {
                "id": "previous",
                "created_at": "2026-10-01T14:59:59.000Z",
                "text": "【 一軍 】 【 阪神 】 previous",
            }
        ]

        result = find_first_matching_post(
            posts,
            local_today=self.today,
            tz=self.tz,
            keyword_1="【 一軍 】",
            keyword_2="【 阪神 】",
        )

        self.assertIsNone(result)


class ConfigTest(unittest.TestCase):
    def test_config_example_is_valid_toml(self) -> None:
        config_path = Path(__file__).with_name("config.toml.example")
        config = load_config(config_path)

        self.assertEqual(config["source"]["username"], "source_account")
        self.assertEqual(config["target"]["username"], "target_account")
        self.assertEqual(config["target"]["post_text"], "yyyy.mm.dd Daily update")
        self.assertEqual(config["system"]["timezone"], "Asia/Tokyo")


class PostTest(unittest.TestCase):
    def test_create_post_sends_only_configured_text(self) -> None:
        client = XClient("consumer", "consumer-secret", "access", "access-secret")
        response = {"data": {"id": "target-post"}}

        with patch.object(client, "_request", return_value=response) as request:
            result = client.create_post("2026.10.02 阪神戦まとめ")

        self.assertEqual(result, response["data"])
        request.assert_called_once_with(
            "POST",
            "/2/tweets",
            body={"text": "2026.10.02 阪神戦まとめ"},
        )

    def test_render_post_text_uses_local_date(self) -> None:
        self.assertEqual(
            render_post_text("yyyy.mm.dd 阪神戦まとめ", date(2026, 10, 2)),
            "2026.10.02 阪神戦まとめ",
        )


class TimelineRequestTest(unittest.TestCase):
    def test_requests_minimum_page_size_and_date_bounds(self) -> None:
        client = XClient("consumer", "consumer-secret", "access", "access-secret")
        start_time = datetime.fromisoformat("2026-10-01T15:00:00+00:00")
        end_time = datetime.fromisoformat("2026-10-02T03:00:00+00:00")

        with patch.object(client, "_request", return_value={"data": []}) as request:
            client.get_timeline_page(
                "source-user",
                start_time=start_time,
                end_time=end_time,
            )

        request.assert_called_once_with(
            "GET",
            "/2/users/source-user/tweets",
            params={
                "max_results": 5,
                "exclude": "retweets,replies",
                "tweet.fields": "created_at,text",
                "start_time": "2026-10-01T15:00:00Z",
                "end_time": "2026-10-02T03:00:00Z",
            },
        )


class OAuth1Test(unittest.TestCase):
    def test_rfc5849_signature_example(self) -> None:
        header = oauth1_authorization_header(
            "GET",
            "http://photos.example.net/photos?file=vacation.jpg&size=original",
            consumer_key="dpf43f3p2l4k3l03",
            consumer_secret="kd94hf93k423kf44",
            access_token="nnch734d00sl2jdk",
            access_token_secret="pfkkdhi9sl3r4s00",
            timestamp="1191242096",
            nonce="kllo9940pd9333jh",
        )

        self.assertIn('oauth_signature="tR3%2BTy81lMeYAr%2FFid0kMTYa%2FWM%3D"', header)


if __name__ == "__main__":
    unittest.main()
