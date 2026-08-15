"""Tests for merging admitted plugins into one composition.

The runtime refuses to boot unless the lock inventory exactly matches the
enabled plugins, so a bad merge is the one way to weaken admission without
tripping it. These pin the cases that would do that quietly.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from .composition import (
    CompositionError,
    composition_plugin_references,
    merge_admissions,
)

POLICY = "a" * 64


def approval(plugin: str, distribution: str, **overrides):
    base = {
        "ok": True,
        "schema_version": 1,
        "plugin": plugin,
        "version": "0.1.0",
        "distribution": distribution,
        "host": "severino-hq",
        "plugin_api_version": 1,
        "source_repository": f"joeseverino/{distribution}",
        "source_workflow": ".github/workflows/admit-plugin.yml",
        "source_commit": "0" * 40,
        "signer_identity": "https://github.com/x/y@refs/heads/main",
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "artifact_sha256": "b" * 64,
        "policy_sha256": POLICY,
    }
    return {**base, **overrides}


class MergeAdmissionsTests(SimpleTestCase):
    def test_merges_independent_approvals_into_one_lock(self):
        lock = merge_admissions(
            [
                approval("example.alpha", "example-alpha"),
                approval("example.beta", "example-beta"),
            ]
        )
        self.assertEqual(lock["host"], "severino-hq")
        self.assertEqual(
            [entry["plugin"] for entry in lock["plugins"]],
            ["example.alpha", "example.beta"],
        )

    def test_output_is_order_independent(self):
        a = approval("example.alpha", "example-alpha")
        b = approval("example.beta", "example-beta")
        # Byte-identical output keeps composed image digests reproducible.
        self.assertEqual(merge_admissions([a, b]), merge_admissions([b, a]))

    def test_approvals_are_carried_verbatim(self):
        entry = approval("example.alpha", "example-alpha")
        merged = merge_admissions([entry])["plugins"][0]
        # Rewriting any field would mean the lock no longer describes what was
        # actually signed.
        self.assertEqual(merged, entry)

    def test_mixed_policies_are_refused(self):
        with self.assertRaisesMessage(CompositionError, "policy_sha256"):
            merge_admissions(
                [
                    approval("example.alpha", "example-alpha"),
                    approval("example.beta", "example-beta", policy_sha256="c" * 64),
                ]
            )

    def test_duplicate_plugin_ids_are_refused(self):
        with self.assertRaisesMessage(CompositionError, "duplicate"):
            merge_admissions(
                [
                    approval("example.alpha", "example-alpha"),
                    approval("example.alpha", "example-alpha"),
                ]
            )

    def test_non_approving_verdict_is_refused(self):
        with self.assertRaisesMessage(CompositionError, "approving verdict"):
            merge_admissions([approval("example.alpha", "example-alpha", ok=False)])

    def test_approval_for_another_host_is_refused(self):
        with self.assertRaisesMessage(CompositionError, "targets host"):
            merge_admissions(
                [approval("example.alpha", "example-alpha", host="other-host")]
            )

    def test_empty_composition_is_refused(self):
        with self.assertRaisesMessage(CompositionError, "at least one"):
            merge_admissions([])


class PluginReferenceTests(SimpleTestCase):
    def test_references_are_derived_from_the_lock(self):
        lock = merge_admissions(
            [
                approval("example.alpha", "example-alpha"),
                approval("example.beta", "example-beta"),
            ]
        )
        self.assertEqual(
            composition_plugin_references(lock),
            "example_alpha.plugin:plugin,example_beta.plugin:plugin",
        )

    def test_reference_set_matches_the_lock_exactly(self):
        # The runtime rejects any mismatch between enabled and approved, so the
        # derived value must never be a subset or superset of the lock.
        lock = merge_admissions(
            [
                approval("example.alpha", "example-alpha"),
                approval("example.beta", "example-beta"),
            ]
        )
        self.assertEqual(
            len(composition_plugin_references(lock).split(",")), len(lock["plugins"])
        )
