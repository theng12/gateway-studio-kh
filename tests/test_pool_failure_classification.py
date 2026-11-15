#!/usr/bin/env python3
"""A pool's aggregate failure code names the pool, not the last member (owner, 2026-11-15).

Live `story-studio-llm`: the free primary stopped at `finish_reason: length`
(`INCOMPLETE_OUTPUT`), one member was offline (`unavailable`), and one returned
`PROVIDER_AUTH_FAILED` — and the job was reported as an auth failure, which reads as "every
member failed auth" and hides the primary cause. The member causes were published all along in
`failure_reason` and `pool_attempts`; the aggregate code was the defect.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation  # noqa: E402

_TMP = _isolation.sandbox_env("gateway-pool-classification-")

from gateway.adapters.pool import pool_failure_code  # noqa: E402


class PoolFailureCodeTests(unittest.TestCase):
    def test_the_live_mixed_outcome_is_pool_exhaustion(self) -> None:
        attempts = [
            {"member": "magpie::group/story-llm-free", "outcome": "failed",
             "code": "INCOMPLETE_OUTPUT", "retryable": True},
            {"member": "openai-direct::gpt-6-luna", "outcome": "unavailable"},
            {"member": "anthropic-direct::claude-haiku-5-5", "outcome": "failed",
             "code": "PROVIDER_AUTH_FAILED", "status": 401},
        ]
        self.assertEqual(pool_failure_code(attempts, "PROVIDER_AUTH_FAILED"), "POOL_EXHAUSTED")

    def test_two_different_member_codes_are_pool_exhaustion(self) -> None:
        attempts = [
            {"member": "a", "outcome": "failed", "code": "INCOMPLETE_OUTPUT"},
            {"member": "b", "outcome": "failed", "code": "PROVIDER_AUTH_FAILED"},
        ]
        self.assertEqual(pool_failure_code(attempts, "PROVIDER_AUTH_FAILED"), "POOL_EXHAUSTED")

    def test_one_member_failure_keeps_its_own_code(self) -> None:
        self.assertEqual(pool_failure_code(
            [{"member": "a", "outcome": "failed", "code": "INCOMPLETE_OUTPUT"}],
            "INCOMPLETE_OUTPUT"), "INCOMPLETE_OUTPUT")
        self.assertEqual(pool_failure_code(
            [{"member": "a", "outcome": "failed", "code": "PROVIDER_AUTH_FAILED"}],
            "PROVIDER_AUTH_FAILED"), "PROVIDER_AUTH_FAILED")

    def test_an_all_auth_pool_still_reads_auth_failed(self) -> None:
        attempts = [
            {"member": "a", "outcome": "failed", "code": "PROVIDER_AUTH_FAILED"},
            {"member": "b", "outcome": "failed", "code": "PROVIDER_AUTH_FAILED"},
        ]
        self.assertEqual(pool_failure_code(attempts, "PROVIDER_AUTH_FAILED"),
                         "PROVIDER_AUTH_FAILED")

    def test_a_short_member_without_its_own_code_is_pool_exhaustion(self) -> None:
        attempts = [{"member": "a", "outcome": "cooling-down", "until": 123.0}]
        self.assertEqual(pool_failure_code(attempts, "PROVIDER_AUTH_FAILED"), "POOL_EXHAUSTED")


if __name__ == "__main__":
    unittest.main()
