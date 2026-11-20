"""Rejects database writes while an agent request or MCP tool runs.

Agent access is promised to be read-only; this enforces it at the session level so the
promise doesn't rest on every service an agent tool calls staying read-only forever.
"""
from contextlib import contextmanager

from flask import g, has_app_context
from sqlalchemy import event
from sqlalchemy.orm import Session


class ReadOnlyViolation(RuntimeError):
    pass


def enable():
    """Make the current request or app context read-only until reset()."""
    g.read_only = True


def reset(_exc=None):
    # Also a teardown_request hook: a test client request can share the caller's app
    # context, so the flag must not outlive the request that set it.
    g.pop('read_only', None)


@contextmanager
def read_only():
    enable()
    try:
        yield
    finally:
        reset()


def _is_read_only():
    return has_app_context() and g.get('read_only', False)


@event.listens_for(Session, 'before_flush')
def _reject_flush(session, flush_context, instances):
    if _is_read_only():
        raise ReadOnlyViolation('Agent access is read-only: a database write was attempted.')


@event.listens_for(Session, 'do_orm_execute')
def _reject_write_statements(orm_execute_state):
    if _is_read_only() and not orm_execute_state.is_select:
        raise ReadOnlyViolation('Agent access is read-only: a database write was attempted.')
