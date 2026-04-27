# -*- coding: utf-8 -*-
"""
Unit tests for deterministic hash-based trace sampling.
"""

import uuid
from unittest import mock

import pytest

from teracron.tracing.sampling import (
    clear_sampling_decision,
    get_sampling_decision,
    set_sampling_decision,
    should_sample,
)


class TestShouldSample:
    """Tests for the should_sample function."""

    def test_rate_1_always_samples(self):
        """rate=1.0 should always return True regardless of trace_id."""
        for _ in range(100):
            assert should_sample(uuid.uuid4().hex, 1.0) is True

    def test_rate_0_never_samples(self):
        """rate=0.0 should always return False regardless of trace_id."""
        for _ in range(100):
            assert should_sample(uuid.uuid4().hex, 0.0) is False

    def test_rate_above_1_always_samples(self):
        """rate > 1.0 should be treated as 1.0 (always sample)."""
        assert should_sample(uuid.uuid4().hex, 1.5) is True
        assert should_sample(uuid.uuid4().hex, 100.0) is True

    def test_rate_below_0_never_samples(self):
        """rate < 0.0 should be treated as 0.0 (never sample)."""
        assert should_sample(uuid.uuid4().hex, -0.1) is False
        assert should_sample(uuid.uuid4().hex, -100.0) is False

    def test_deterministic_same_trace_id(self):
        """Same trace_id + rate should always produce the same decision."""
        trace_id = uuid.uuid4().hex
        result = should_sample(trace_id, 0.5)
        for _ in range(100):
            assert should_sample(trace_id, 0.5) == result

    def test_different_trace_ids_vary(self):
        """Different trace_ids should produce varied results at rate=0.5."""
        results = set()
        for _ in range(200):
            results.add(should_sample(uuid.uuid4().hex, 0.5))
        # At rate=0.5 over 200 samples, we should see both True and False.
        assert True in results
        assert False in results

    def test_distribution_at_50_percent(self):
        """
        At rate=0.5 over 10K samples, ~50% should be sampled (±10%).
        This is a statistical test — generous tolerance for CI stability.
        """
        sampled = sum(
            should_sample(uuid.uuid4().hex, 0.5) for _ in range(10_000)
        )
        # Expect ~5000, allow 4000–6000 (±10%)
        assert 4000 <= sampled <= 6000, f"Expected ~5000, got {sampled}"

    def test_distribution_at_10_percent(self):
        """At rate=0.1, ~10% should be sampled."""
        sampled = sum(
            should_sample(uuid.uuid4().hex, 0.1) for _ in range(10_000)
        )
        # Expect ~1000, allow 700–1300 (±3%)
        assert 700 <= sampled <= 1300, f"Expected ~1000, got {sampled}"

    def test_distribution_at_90_percent(self):
        """At rate=0.9, ~90% should be sampled."""
        sampled = sum(
            should_sample(uuid.uuid4().hex, 0.9) for _ in range(10_000)
        )
        # Expect ~9000, allow 8700–9300
        assert 8700 <= sampled <= 9300, f"Expected ~9000, got {sampled}"


class TestSamplingDecisionContextVar:
    """Tests for the sampling decision ContextVar."""

    def setup_method(self):
        clear_sampling_decision()

    def teardown_method(self):
        clear_sampling_decision()

    def test_default_is_none(self):
        assert get_sampling_decision() is None

    def test_set_true(self):
        set_sampling_decision(True)
        assert get_sampling_decision() is True

    def test_set_false(self):
        set_sampling_decision(False)
        assert get_sampling_decision() is False

    def test_clear_resets_to_none(self):
        set_sampling_decision(True)
        clear_sampling_decision()
        assert get_sampling_decision() is None
