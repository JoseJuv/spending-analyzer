import pytest

from spending_analyzer.store import Store


@pytest.fixture
def store():
    return Store()
