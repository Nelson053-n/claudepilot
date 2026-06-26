"""
Фикстуры для тестов prof.

Токен задаётся через PROF_TOKEN ДО импорта app, чтобы он был детерминирован
и не зависел от prof.db (get_token() читает env в первую очередь).
"""
import os
import sys
from pathlib import Path

import pytest

# корень проекта в sys.path + фиксированный токен до импорта app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["PROF_TOKEN"] = "test-token-fixed"

from fastapi.testclient import TestClient  # noqa: E402

import app as prof_app  # noqa: E402

TOKEN = os.environ["PROF_TOKEN"]


@pytest.fixture
def client():
    with TestClient(prof_app.app) as c:
        yield c
