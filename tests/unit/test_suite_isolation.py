"""That the suite cannot reach the databases the application is using.

Both halves of this were learned the hard way. Postgres was isolated from the
start because tests drop the schema, and that damage would be obvious. Redis was
not, because flushing a cache looks harmless -- and the damage turned out to be
worse: an arq queue is Redis keys, so a development worker on the same database
picks up jobs the tests enqueued, runs them against its own Postgres, and the
test's burst worker finds nothing left to do. Three worker tests then fail about
one run in ten, in three different places, none of them near the cause.
"""

import os

import pytest

from tests.conftest import (
    LOCAL_REDIS_URL,
    TEST_DATABASE_URL,
    TEST_REDIS_DB,
    TEST_REDIS_URL,
    _redis_target,
    _test_redis_url,
)


def test_the_suite_does_not_share_a_redis_database_with_the_application() -> None:
    app_redis = os.getenv("REDIS_URL", LOCAL_REDIS_URL)

    assert _redis_target(TEST_REDIS_URL) != _redis_target(app_redis)


def test_the_suite_does_not_share_a_postgres_database_with_the_application() -> None:
    assert TEST_DATABASE_URL.rsplit("/", 1)[-1].endswith("_test")
    assert os.getenv("DATABASE_URL") != TEST_DATABASE_URL


def test_the_redis_database_is_redirected_rather_than_inherited() -> None:
    """The default has to move the database number, not just pass REDIS_URL on."""
    assert _redis_target(TEST_REDIS_URL)[1] == str(TEST_REDIS_DB)


def test_an_explicit_test_url_is_taken_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI passes one in, and it is not this module's place to rewrite it."""
    monkeypatch.setenv("TEST_REDIS_URL", "redis://elsewhere:6379/3")

    assert _test_redis_url() == "redis://elsewhere:6379/3"


def test_a_url_with_no_database_number_still_gets_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://somewhere:6379")

    assert _test_redis_url() == f"redis://somewhere:6379/{TEST_REDIS_DB}"


def test_two_spellings_of_the_same_database_are_recognised_as_one() -> None:
    """`redis://h:6379` and `redis://h:6379/0` are one place; the guard must know."""
    assert _redis_target("redis://h:6379") == _redis_target("redis://h:6379/0")
    assert _redis_target("redis://h:6379/0") != _redis_target("redis://h:6379/15")
