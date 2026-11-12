from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from bot.config import Settings
from bot.db import Database
from bot.http import HttpClient
from bot.models import Job

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)


def load_fixture(name: str) -> Any:
    path = FIXTURES / name
    return json.loads(path.read_text(encoding="utf-8")) if name.endswith(".json") else path.read_text(encoding="utf-8")


class Router:
    """Tiny httpx mock: register (method, regex) -> response or callable. Records calls."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern, Any]] = []
        self.calls: list[httpx.Request] = []

    def add(self, method: str, pattern: str, response: Any) -> "Router":
        self.routes.append((method.upper(), re.compile(pattern), response))
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        for method, rx, resp in self.routes:
            if method == request.method and rx.search(url):
                if callable(resp):
                    resp = resp(request)
                if isinstance(resp, Exception):
                    raise resp
                if isinstance(resp, httpx.Response):
                    return resp
                if isinstance(resp, str):
                    return httpx.Response(200, text=resp)
                return httpx.Response(200, json=resp)
        return httpx.Response(404, json={"error": "no route", "url": url})

    def count(self, pattern: str) -> int:
        return sum(1 for r in self.calls if re.search(pattern, str(r.url)))


async def _no_sleep(_: float) -> None:
    return None


def make_http(router: Router, max_retries: int = 2) -> HttpClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(router.handler))
    return HttpClient(client, max_retries=max_retries, backoff_base=0.0, sleep=_no_sleep)


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    s.max_dms_per_scan = 50
    s.baseline_notify_limit = 100
    return s


@pytest.fixture
def db(settings) -> Database:
    d = Database(settings.database_path)
    yield d
    d.close()


def make_job(**kw) -> Job:
    base = dict(source="test:board", source_job_id="1", company="Acme", title="Software Engineer Intern",
                url="https://jobs.example.com/acme/1", location="Toronto, ON, Canada")
    base.update(kw)
    return Job(**base)


@pytest.fixture
def job_factory() -> Callable[..., Job]:
    return make_job
