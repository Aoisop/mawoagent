import unittest

from parsing import parse_duration


class ParseDurationTests(unittest.TestCase):
    def test_whole_seconds(self):
        self.assertEqual(parse_duration("45s"), 45)

    def test_whole_minutes(self):
        self.assertEqual(parse_duration("3m"), 180)

    def test_unknown_suffix_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_duration("5h")


if __name__ == "__main__":
    unittest.main()
