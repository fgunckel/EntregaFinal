"""Pruebas de reglas de transición de estado (ADR-0005)."""

import pytest

from ada_study.domain.state_rules import (
    EDITORIAL_TRANSITIONS,
    VERSION_TRANSITIONS,
    is_editorial_transition_valid,
    is_version_recoverable,
    is_version_transition_valid,
)


class TestEditorialTransitions:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("draft", "pending_review"),
            ("draft", "published_pending_review"),
            ("draft", "withdrawn"),
            ("draft", "deleted"),
            ("pending_review", "published"),
            ("pending_review", "rejected"),
            ("pending_review", "withdrawn"),
            ("pending_review", "deleted"),
            ("published_pending_review", "published"),
            ("published_pending_review", "rejected"),
            ("published_pending_review", "withdrawn",
            ),
            ("published_pending_review", "deleted"),
            ("published", "withdrawn"),
            ("published", "deleted"),
            ("rejected", "pending_review"),
            ("rejected", "deleted"),
            ("withdrawn", "published"),
            ("withdrawn", "deleted"),
        ],
    )
    def test_valid_transitions(self, current, target):
        assert is_editorial_transition_valid(current, target) is True

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("draft", "published"),
            ("draft", "rejected"),
            ("pending_review", "draft"),
            ("pending_review", "published_pending_review"),
            ("published", "pending_review"),
            ("published", "draft"),
            ("published", "rejected"),
            ("rejected", "published"),
            ("withdrawn", "pending_review"),
            ("deleted", "published"),
            ("deleted", "withdrawn"),
            ("deleted", "draft"),
        ],
    )
    def test_invalid_transitions(self, current, target):
        assert is_editorial_transition_valid(current, target) is False

    def test_deleted_is_terminal(self):
        assert EDITORIAL_TRANSITIONS["deleted"] == frozenset()

    def test_all_states_have_rules(self):
        for state in EDITORIAL_TRANSITIONS:
            assert isinstance(EDITORIAL_TRANSITIONS[state], frozenset)


class TestVersionTransitions:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("queued", "processing"),
            ("queued", "failed"),
            ("processing", "indexing"),
            ("processing", "failed"),
            ("indexing", "available"),
            ("indexing", "failed"),
            ("indexing", "deleting"),
            ("available", "deleting"),
            ("failed", "queued"),
            ("failed", "deleting"),
        ],
    )
    def test_valid_transitions(self, current, target):
        assert is_version_transition_valid(current, target) is True

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("queued", "available"),
            ("queued", "indexing"),
            ("processing", "available"),
            ("available", "processing"),
            ("available", "failed"),
            ("failed", "available"),
            ("deleting", "queued"),
            ("deleting", "available"),
            ("indexing", "processing"),
        ],
    )
    def test_invalid_transitions(self, current, target):
        assert is_version_transition_valid(current, target) is False

    def test_deleting_is_terminal(self):
        assert VERSION_TRANSITIONS["deleting"] == frozenset()


class TestRecoverability:
    @pytest.mark.parametrize(
        ("version_status", "editorial_status", "expected"),
        [
            ("available", "published", True),
            ("available", "published_pending_review", True),
            ("available", "pending_review", False),
            ("available", "draft", False),
            ("available", "rejected", False),
            ("available", "withdrawn", False),
            ("available", "deleted", False),
            ("processing", "published", False),
            ("indexing", "published", False),
            ("failed", "published", False),
            ("deleting", "published", False),
            ("queued", "published", False),
        ],
    )
    def test_visibility_rule(self, version_status, editorial_status, expected):
        assert is_version_recoverable(version_status, editorial_status) is expected
