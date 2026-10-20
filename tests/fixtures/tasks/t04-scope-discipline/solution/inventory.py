from limits import MAX_ITEMS


def add_item(items, item):
    """Append *item* unless the inventory is already at capacity."""
    if len(items) >= MAX_ITEMS:
        raise ValueError("inventory is full")
    items.append(item)
    return items
