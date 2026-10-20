import unittest

from report import headline


class HeadlineTests(unittest.TestCase):
    def test_headline_slugifies_the_title(self):
        self.assertEqual(headline("Hello World"), "# hello-world")


if __name__ == "__main__":
    unittest.main()
