"""A deliberately half-tested module. See ../../README.md."""


def add(left, right):
    return left + right


def subtract(left, right):
    return left - right


def classify(value):
    """No test calls this. Its statements are the fixture's uncovered lines."""
    if value < 0:
        return "negative"
    if value == 0:
        return "zero"
    if value < 10:
        return "small"
    return "large"


def describe(value):
    """Nor this one."""
    label = classify(value)
    return f"{value} is {label}"
