from utils import slugify


def headline(title):
    return f"# {slugify(title)}"
