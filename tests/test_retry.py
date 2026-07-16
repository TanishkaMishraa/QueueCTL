import pytest

from queuectl import retry


@pytest.mark.parametrize(
    "attempts, base, expected",
    [
        (1, 2, 2),
        (2, 2, 4),
        (3, 2, 8),
        (4, 2, 16),
        (1, 3, 3),
        (2, 3, 9),
    ],
)
def test_calculate_delay_sequence(attempts, base, expected):
    assert retry.calculate_delay(attempts, base) == expected


@pytest.mark.parametrize(
    "attempts, max_retries, expected",
    [
        (0, 3, False),
        (1, 3, False),
        (2, 3, False),
        (3, 3, True),
        (4, 3, True),
        (1, 1, True),
        (0, 0, True),
    ],
)
def test_is_dead(attempts, max_retries, expected):
    assert retry.is_dead(attempts, max_retries) is expected
