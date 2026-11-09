import unittest

from utils.links import message_link, parse_telegram_link


class LinkParsingTest(unittest.TestCase):
    def test_public_link(self):
        link = parse_telegram_link("https://t.me/SSC_CGL_DISCUSSION_GROUP_2026_27/42966")
        self.assertIsNotNone(link)
        self.assertEqual(link.chat, "SSC_CGL_DISCUSSION_GROUP_2026_27")
        self.assertEqual(link.message_id, 42966)
        self.assertFalse(link.is_private)

    def test_private_link(self):
        link = parse_telegram_link("https://t.me/c/3891933514/1428")
        self.assertIsNotNone(link)
        self.assertEqual(link.chat, -1003891933514)
        self.assertEqual(link.message_id, 1428)
        self.assertTrue(link.is_private)

    def test_preview_and_query(self):
        link = parse_telegram_link("https://t.me/s/channel_name/77?single")
        self.assertIsNotNone(link)
        self.assertEqual(link.chat, "channel_name")
        self.assertEqual(link.message_id, 77)

    def test_tg_scheme(self):
        link = parse_telegram_link("tg://resolve?domain=my_channel&post=55")
        self.assertIsNotNone(link)
        self.assertEqual(link.chat, "my_channel")
        self.assertEqual(link.message_id, 55)

    def test_link_inside_text(self):
        link = parse_telegram_link("ye lo link https://t.me/abc/12 dekho")
        self.assertIsNotNone(link)
        self.assertEqual(link.message_id, 12)

    def test_invalid_links(self):
        self.assertIsNone(parse_telegram_link("https://t.me/+invitehash"))
        self.assertIsNone(parse_telegram_link("https://example.com/abc/12"))
        self.assertIsNone(parse_telegram_link("hello world"))

    def test_message_link_builder(self):
        self.assertEqual(
            message_link(-1003891933514, 1428),
            "https://t.me/c/3891933514/1428",
        )
        self.assertEqual(
            message_link(-1003891933514, 1428, "my_channel"),
            "https://t.me/my_channel/1428",
        )


if __name__ == "__main__":
    unittest.main()
