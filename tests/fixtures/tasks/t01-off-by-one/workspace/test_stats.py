import unittest

from stats import mean


class MeanTests(unittest.TestCase):
    def test_mean_of_two_values(self):
        self.assertEqual(mean([2, 4]), 3.0)

    def test_mean_of_one_value(self):
        self.assertEqual(mean([7]), 7.0)


if __name__ == "__main__":
    unittest.main()
