# -*- coding: utf-8 -*-
"""Unit tests for trace context — Phase 2: span stack, nesting, cross-process."""

import asyncio
import threading

import pytest

from teracron.tracing.context import (
    clear_trace,
    get_trace_header,
    get_trace_id,
    peek_parent_span_id,
    pop_span,
    push_span,
    set_trace_header,
    start_trace,
)


class TestTraceContext:
    """Basic trace context lifecycle tests."""

    def setup_method(self):
        clear_trace()

    def teardown_method(self):
        clear_trace()

    def test_no_trace_returns_none(self):
        assert get_trace_id() is None

    def test_start_trace_returns_string(self):
        tid = start_trace()
        assert isinstance(tid, str)
        assert len(tid) == 32

    def test_get_trace_id_after_start(self):
        tid = start_trace()
        assert get_trace_id() == tid

    def test_clear_trace_resets_to_none(self):
        start_trace()
        clear_trace()
        assert get_trace_id() is None

    def test_start_trace_generates_unique_ids(self):
        t1 = start_trace()
        clear_trace()
        t2 = start_trace()
        assert t1 != t2

    def test_start_trace_overwrites_previous(self):
        t1 = start_trace()
        t2 = start_trace()
        assert t1 != t2
        assert get_trace_id() == t2

    def test_start_trace_resets_span_stack(self):
        start_trace()
        push_span("span_a")
        assert peek_parent_span_id() == "span_a"
        # Starting a new trace must reset the stack.
        start_trace()
        assert peek_parent_span_id() is None


class TestSpanStack:
    """Phase 2: span stack for parent-child nesting."""

    def setup_method(self):
        clear_trace()

    def teardown_method(self):
        clear_trace()

    def test_no_parent_initially(self):
        start_trace()
        assert peek_parent_span_id() is None

    def test_push_span_sets_parent(self):
        start_trace()
        push_span("span_a")
        assert peek_parent_span_id() == "span_a"

    def test_nested_push_pop(self):
        start_trace()
        push_span("root")
        push_span("child")
        assert peek_parent_span_id() == "child"
        popped = pop_span()
        assert popped == "child"
        assert peek_parent_span_id() == "root"
        pop_span()
        assert peek_parent_span_id() is None

    def test_pop_empty_returns_none(self):
        start_trace()
        assert pop_span() is None

    def test_deep_nesting(self):
        start_trace()
        for i in range(10):
            push_span(f"span_{i}")
        assert peek_parent_span_id() == "span_9"
        for i in range(9, -1, -1):
            assert pop_span() == f"span_{i}"
        assert peek_parent_span_id() is None

    def test_clear_trace_clears_stack(self):
        start_trace()
        push_span("span_a")
        push_span("span_b")
        clear_trace()
        assert peek_parent_span_id() is None


class TestTraceContextThreadIsolation:
    """Verify that ContextVars are isolated across OS threads."""

    def setup_method(self):
        clear_trace()

    def teardown_method(self):
        clear_trace()

    def test_threads_get_independent_contexts(self):
        results = {}
        barrier = threading.Barrier(2)

        def worker(name):
            tid = start_trace()
            push_span(f"span_{name}")
            barrier.wait()  # Sync: both threads have set their trace IDs
            results[name] = (get_trace_id(), peek_parent_span_id())
            assert results[name][0] == tid
            clear_trace()

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert results["a"][0] != results["b"][0]
        assert results["a"][1] == "span_a"
        assert results["b"][1] == "span_b"

    def test_main_thread_unaffected_by_child(self):
        main_tid = start_trace()
        push_span("main_span")

        child_tid_holder = {}

        def child():
            child_tid_holder["tid"] = start_trace()
            push_span("child_span")
            clear_trace()

        t = threading.Thread(target=child)
        t.start()
        t.join(timeout=5)

        # Main thread trace and stack should be untouched
        assert get_trace_id() == main_tid
        assert peek_parent_span_id() == "main_span"
        assert main_tid != child_tid_holder["tid"]
        clear_trace()


class TestTraceContextAsyncIsolation:
    """Verify ContextVar isolation across asyncio tasks."""

    def test_async_tasks_get_independent_contexts(self):
        results = {}

        async def run():
            async def worker(name):
                tid = start_trace()
                push_span(f"span_{name}")
                await asyncio.sleep(0)  # Yield — force context snapshot
                results[name] = (get_trace_id(), peek_parent_span_id())
                assert results[name][0] == tid
                clear_trace()

            await asyncio.gather(worker("a"), worker("b"))

        asyncio.get_event_loop().run_until_complete(run())
        assert results["a"][0] != results["b"][0]
        assert results["a"][1] == "span_a"
        assert results["b"][1] == "span_b"

    def test_async_parent_context_preserved(self):
        parent_tid = None
        child_tid = None

        async def run():
            nonlocal parent_tid, child_tid
            parent_tid = start_trace()

            async def child():
                nonlocal child_tid
                child_tid = start_trace()
                clear_trace()

            await child()

        asyncio.get_event_loop().run_until_complete(run())
        assert parent_tid is not None
        assert child_tid is not None
        assert parent_tid != child_tid


class TestCrossProcessPropagation:
    """Phase 2: get_trace_header / set_trace_header."""

    def setup_method(self):
        clear_trace()

    def teardown_method(self):
        clear_trace()

    def test_get_header_none_when_no_trace(self):
        assert get_trace_header() is None

    def test_get_header_trace_id_only(self):
        tid = start_trace()
        header = get_trace_header()
        assert header == tid

    def test_get_header_with_parent_span(self):
        tid = start_trace()
        push_span("a" * 32)
        header = get_trace_header()
        assert header == f"{tid}:{'a' * 32}"

    def test_set_header_trace_id_only(self):
        trace_id = "a" * 32
        set_trace_header(trace_id)
        assert get_trace_id() == trace_id
        assert peek_parent_span_id() is None

    def test_set_header_with_parent_span(self):
        trace_id = "b" * 32
        parent_id = "c" * 32
        set_trace_header(f"{trace_id}:{parent_id}")
        assert get_trace_id() == trace_id
        assert peek_parent_span_id() == parent_id

    def test_set_header_none_ignored(self):
        start_trace()
        original_tid = get_trace_id()
        set_trace_header(None)
        assert get_trace_id() == original_tid

    def test_set_header_empty_ignored(self):
        start_trace()
        original_tid = get_trace_id()
        set_trace_header("")
        assert get_trace_id() == original_tid

    def test_set_header_invalid_trace_id_ignored(self):
        start_trace()
        original_tid = get_trace_id()
        set_trace_header("not-hex-not-32-chars")
        assert get_trace_id() == original_tid

    def test_set_header_invalid_parent_id_ignored(self):
        trace_id = "d" * 32
        set_trace_header(f"{trace_id}:bad")
        assert get_trace_id() == trace_id
        assert peek_parent_span_id() is None

    def test_roundtrip_propagation(self):
        """Simulate outbound → inbound header propagation."""
        # Service A
        tid = start_trace()
        push_span("e" * 32)
        header = get_trace_header()
        clear_trace()

        # Service B — restore from header
        set_trace_header(header)
        assert get_trace_id() == tid
        assert peek_parent_span_id() == "e" * 32

    def test_set_header_non_string_ignored(self):
        start_trace()
        original_tid = get_trace_id()
        set_trace_header(12345)  # type: ignore
        assert get_trace_id() == original_tid

    def test_set_header_invalid_hex_in_parent(self):
        trace_id = "f" * 32
        set_trace_header(f"{trace_id}:{'g' * 32}")  # 'g' is not hex
        assert get_trace_id() == trace_id
        assert peek_parent_span_id() is None
