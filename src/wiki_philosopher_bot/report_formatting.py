"""Shared compact formatting for historical death-report presentation."""


def compact_percentage(value, *, missing="n/a"):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return missing
    rounded = round(float(value), 1)
    return "{}%".format(
        int(rounded) if rounded.is_integer() else "{:.1f}".format(rounded)
    )


def mean_age(value, *, missing="n/a"):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return missing
    return "{:.1f}".format(float(value))


def median_age(value, *, missing="n/a"):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return missing
    return "{:g}".format(value)
