from sample.calc import add, classify, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2


def test_classify_small():
    # Only the one branch, deliberately: `describe` and classify's remaining returns stay
    # uncovered so the fixture sits between the two floors the self-test exercises.
    assert classify(5) == "small"
