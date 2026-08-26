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


def metric(value, unit=""):
    """Format a number with SI prefix.

    Examples: metric(25e6, "Hz") -> "25MHz", metric(0.001, "A") -> "1mA"
    """
    if isinstance(value, (int, float)):
        value = Decimal(value)
    if not isinstance(value, Decimal):
        return value
    if value == 0:
        return "0" + unit

    if value < 0:
        value = -value
        s = "-"
    else:
        s = ""
    exp = 0
    if value < 1:
        while value * Decimal((0, (1,), exp)) < 1:
            exp += 3
    else:
        while value * Decimal((0, (1,), exp)) >= 10000:
            exp -= 3
    m = value * Decimal((0, (1,), exp))
    scale = "TGMk mµnpf"
    suffix = scale[4 + exp // 3]
    if suffix == ' ':
        suffix = ''
    rm = int(float(m * 1000) + .5) / 1000.
    sm = ('%f' % rm).rstrip('0').rstrip('.')
    return s + sm + suffix + unit


def base2(value, unit=""):
    """Format a number with binary prefix.

    Examples: base2(4194304, "B") -> "4MiB", base2(1024, "B") -> "1kiB"
    """
    if value <= 1:
        return str(value) + unit

    if isinstance(value, (int, float, Decimal)):
        value = int(value)

    exp = 0
    while (value >> exp) >= 1024:
        exp += 10
    m = value * (2. ** -exp)
    scale = ["", "ki", "Mi", "Gi"]
    exp = min((len(scale) - 1) * 10, exp)
    suffix = scale[exp // 10]

    return ('%f' % (int(float(m * 1024) + .5) / 1024)).rstrip('0').rstrip('.') + suffix + unit


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


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def bool_parse(value):
    """Parse a path-option value into a bool.

    Accepts the spellings a user would reasonably type on a command
    line: "1"/"true"/"yes"/"on" and their negatives, any case.
    """
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(
        f"expected one of {', '.join(_TRUE + _FALSE)}, got {value!r}")

