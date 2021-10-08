import importlib
import os
import pkgutil
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from functools import wraps
from inspect import getmembers, getmodule, isclass, isfunction, ismethod, ismodule
from itertools import zip_longest
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional, Type

import sqlalchemy.event

from redun import Scheduler


def clean_dir(path: str) -> None:
    """
    Ensure path exists and is an empty directory.
    """
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path)


def get_test_file(filename: str) -> str:
    """
    Returns a file from test_data.
    """
    basedir = os.path.dirname(__file__)
    return os.path.join(basedir, filename)


def use_tempdir(func: Callable) -> Callable:
    """
    Run function within a temporary directory.
    """

    @wraps(func)
    def wrap(*args: Any, **kwargs: Any) -> Any:
        with tempfile.TemporaryDirectory() as tmpdir:
            original_dir = os.getcwd()
            os.chdir(tmpdir)

            try:
                result = func(*args, **kwargs)
            finally:
                os.chdir(original_dir)
        return result

    return wrap


def assert_match_lines(patterns: List[str], lines: List[str]) -> None:
    """
    Asserts whether `lines` match `patterns`.
    """
    assert len(patterns) == len(lines)
    for pattern, line in zip(patterns, lines):
        assert re.fullmatch(pattern, line)


def assert_match_text(pattern: str, text: str, wildcard: str = "*"):
    """
    Assert whether two strings are equal using wildcards.
    """
    for i, (a, b) in enumerate(zip_longest(pattern, text)):
        if a != b and a != wildcard:
            assert False, "mismatch on character {}: '{}' != '{}'".format(
                i, pattern[: i + 1], text[: i + 1]
            )


def wait_until(cond: Callable[[], bool], interval: float = 0.02, timeout: float = 1.0) -> None:
    """
    Wait until `cond()` is True or timeout is exceeded.
    """
    start = time.time()
    while not cond():
        if time.time() - start > timeout:
            raise RuntimeError("Timeout")
        time.sleep(interval)


class MatchEnv:
    """

    An environment for generating Match objects.
    """

    def __init__(self):
        self.vars: Dict[str, Any] = {}

    def match(self, *args, **kwargs) -> "Match":
        kwargs["env"] = self
        return Match(*args, **kwargs)


class Match:
    """
    Helper for asserting values have particular properties (types, etc).
    """

    def __init__(
        self,
        type: Optional[Type] = None,
        var: Optional[str] = None,
        regex: Optional[str] = None,
        any: bool = True,
        env: Optional[MatchEnv] = None,
    ):
        self.any = any
        self.type = type
        self.var = var
        self.regex = regex
        self.env = env

    def __repr__(self) -> str:
        if self.var:
            return "Match(var={})".format(self.var)
        elif self.type:
            return "Match(type={})".format(self.type.__name__)
        elif self.regex:
            return "Match(regex={})".format(self.regex)
        elif self.any:
            return "Match(any=True)"
        else:
            return "Match()"

    def __eq__(self, other: Any) -> bool:
        if self.env and self.var:
            # First instance of var will always return True.
            # Second instance of var has to match previous value.
            expected = self.env.vars.setdefault(self.var, other)
            if expected != other:
                return False

        if self.type:
            return isinstance(other, self.type)

        elif self.regex:
            return bool(re.fullmatch(self.regex, other))

        else:
            return self.any


class QueryStats(NamedTuple):
    """
    Stats for a recorded SQLAlchemy query.
    """

    statement: str
    parameters: tuple
    duration: float


@contextmanager
def listen_queries(engine: Any) -> Iterator[List[QueryStats]]:
    """
    Context for capturing SQLAlchemy queries.

    .. code-block:: python

        with listen_queries(engine) as queries:
            result = session.query(Model).filter(...)
            # More SQLAlchemy queries...

        # queries now has a list of statement and parameter tuples.
        assert len(queries) == 2
    """
    queries = []
    cursors = {}

    def before(conn, cursor, statement, parameters, context, executemany):
        cursors[cursor] = time.time()

    def after(conn, cursor, statement, parameters, context, executemany):
        duration = time.time() - cursors.pop(cursor)
        queries.append(QueryStats(statement, parameters, duration))

    sqlalchemy.event.listen(engine, "before_cursor_execute", before)
    sqlalchemy.event.listen(engine, "after_cursor_execute", after)

    yield queries

    sqlalchemy.event.remove(engine, "before_cursor_execute", before)
    sqlalchemy.event.remove(engine, "after_cursor_execute", after)


def import_all_modules(pkg):
    """Import (almost) all modules within a package.

    Ignores explicitly marked modules.
    """
    ignored_modules = ("redun.backends.db.alembic.env",)  # https://stackoverflow.com/a/52575218
    modules = []
    for _, module_name, is_pkg in pkgutil.iter_modules(pkg.__path__):
        full_name = f"{pkg.__name__}.{module_name}"
        if full_name in ignored_modules:
            continue

        module = importlib.import_module(full_name)
        if is_pkg:
            modules.extend(import_all_modules(module))
        else:
            modules.append(module)

    return modules


def get_docstring_owners_in_module(module):
    """Get all functions, classes and their methods defined within a python module.

    Returns
    -------
    docstring_owners : set
        Set of functions, classes and methods
    """
    assert ismodule(module), f"Passed {module.__name__} which is not a module."

    def is_valid(obj):
        if getmodule(obj) == module:
            if ismethod(obj) or isfunction(obj):
                return not obj.__name__.startswith("_")
            if isclass(obj):
                return True
        return False

    to_check = {obj for _, obj in getmembers(module) if is_valid(obj)}
    docstring_owners = set()
    seen = set()

    while to_check:
        candidate = to_check.pop()
        if candidate in seen:
            continue

        if isfunction(candidate) or ismethod(candidate):
            docstring_owners.add(candidate)

        if isclass(candidate):
            to_check.update({obj for _, obj in getmembers(candidate) if is_valid(obj)})

        seen.add(candidate)
    return docstring_owners


def docstring_owner_pretty_name(docstring_owner):
    return ".".join((docstring_owner.__module__, docstring_owner.__qualname__))


def mock_scheduler():
    """
    Returns a scheduler with mocks for job completion.
    """
    # Setup scheduler callbacks.
    scheduler = Scheduler()

    scheduler.job_results = {}
    scheduler.job_errors = {}

    def done_job(job, result, job_tags=[]):
        job.job_tags.extend(job_tags)
        scheduler.job_results[job.id] = result

    def reject_job(job, error, error_traceback=None, job_tags=[]):
        if job:
            job.job_tags.extend(job_tags)
            scheduler.job_errors[job.id] = error
        else:
            # Scheduler error, reraise it.
            raise error

    def batch_wait(job_ids):
        while not all(
            job_id in scheduler.job_results or job_id in scheduler.job_errors for job_id in job_ids
        ):
            time.sleep(0.1)

    scheduler.done_job = done_job
    scheduler.reject_job = reject_job
    scheduler.batch_wait = batch_wait

    return scheduler