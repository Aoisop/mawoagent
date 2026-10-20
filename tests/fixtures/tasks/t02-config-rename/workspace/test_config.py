import unittest

from cli import describe
from client import request_timeout, retries


class ConfigKeyTests(unittest.TestCase):
    def test_timeout_reads_the_renamed_key(self):
        self.assertEqual(request_timeout(), 30)

    def test_unrelated_key_is_untouched(self):
        self.assertEqual(retries(), 3)

    def test_description_uses_the_renamed_key(self):
        self.assertEqual(describe(), "timeout=30s retries=3")


if __name__ == "__main__":
    unittest.main()
