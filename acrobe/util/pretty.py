from decimal import Decimal


_SUFFIX = {
    "T": 12,
    "G": 9,
    "M": 6,
    "k": 3,
    "K": 3,
    "m": -3,
    "\u00b5": -6,
    "u": -6,
    "n": -9,
    "p": -12,
    "f": -15,
}


def sci_parse(string):
    """Parse a metric-suffix string into a number.

    Examples: "10M" -> 10000000, "1.5k" -> 1500, "100" -> 100
    """
    assert string

    if string[-1] in _SUFFIX:
        exp = _SUFFIX[string[-1]]
        string = string[:-1]
    else:
        exp = 0
    return float(Decimal(string) * Decimal((0, (1,), exp)))
