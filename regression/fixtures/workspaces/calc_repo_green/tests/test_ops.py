import pytest

from calc.ops import add, divide, multiply, subtract


def test_add() -> None:
    assert add(2, 3) == 5


def test_subtract() -> None:
    assert subtract(5, 3) == 2


def test_multiply() -> None:
    assert multiply(4, 3) == 12


def test_divide() -> None:
    assert divide(9, 3) == 3


def test_divide_by_zero_raises_value_error() -> None:
    with pytest.raises(ValueError, match="division by zero"):
        divide(1, 0)
