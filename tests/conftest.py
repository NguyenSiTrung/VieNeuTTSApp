"""Shared unit-test fixtures.

Qt allows only one QCoreApplication per process; tests that touch QObject
signals request this session-wide instance instead of building their own.
"""

import pytest
from PySide6.QtCore import QCoreApplication


@pytest.fixture()
def qcoreapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app
