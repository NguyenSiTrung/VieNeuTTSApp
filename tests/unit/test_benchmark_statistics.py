import pytest
from scripts.benchmarks.statistics import summarize


def test_distribution_uses_nearest_rank_percentiles() -> None:
    result = summarize([1.0, 2.0, 3.0, 4.0, 100.0])

    assert result.count == 5
    assert result.minimum == 1.0
    assert result.median == 3.0
    assert result.p90 == 100.0
    assert result.p95 == 100.0
    assert result.maximum == 100.0
    assert result.mad == 1.0


def test_empty_input_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        summarize([])


def test_one_value_distribution_is_that_value() -> None:
    result = summarize([4.5])

    assert result.count == 1
    assert result.minimum == 4.5
    assert result.median == 4.5
    assert result.p90 == 4.5
    assert result.p95 == 4.5
    assert result.maximum == 4.5
    assert result.mad == 0.0
