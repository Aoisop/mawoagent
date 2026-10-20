import unittest

from inventory import add_item
from limits import MAX_ITEMS


class CapacityTests(unittest.TestCase):
    def test_fills_exactly_to_capacity(self):
        items = []
        for index in range(MAX_ITEMS):
            add_item(items, index)
        self.assertEqual(len(items), MAX_ITEMS)

    def test_rejects_item_over_capacity(self):
        items = list(range(MAX_ITEMS))
        with self.assertRaises(ValueError):
            add_item(items, "extra")


if __name__ == "__main__":
    unittest.main()
