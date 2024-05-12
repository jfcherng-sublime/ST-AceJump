# modified from https://pypi.org/project/full-width-to-half-width

FULL_TO_HALF_TABLE = {i + 0xFEE0: i for i in range(0x21, 0x7F)}
HALF_TO_FULL_TABLE = {i: i + 0xFEE0 for i in range(0x21, 0x7F)}


def f2h(string: str) -> str:
    """Convert into half-width."""
    return string.translate(FULL_TO_HALF_TABLE)


def h2f(string: str) -> str:
    """Convert into full-width."""
    return string.translate(HALF_TO_FULL_TABLE)
