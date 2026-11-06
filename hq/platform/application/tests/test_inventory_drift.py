"""When a declaration and what a provider holds differ, and how HQ says so."""

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.providers import PROVIDERS, validate_spec
from hq.platform.core.models import AuditLog

from ..inventory import confirm_observed
from ..record_errors import RecordNotFound
from ..security import cli_principal
from ..sweep import record_sweep
from .inventory_records import A_PROXY, a_sweep


def _adoptable():
    """Every provider a sweep can rebuild a spec for, with its sample record."""

    return [(kind, p) for kind, p in sorted(PROVIDERS.items()) if p.from_record and p.sample_record]


class DriftIsSaidOutLoudTests(TestCase):
    """A sweep that finds a contradiction has to report one.

    The declaration does not keep the condition from the last time it matched.
    """

    def setUp(self):
        self.resource = ManagedResource.objects.create(
            key="a-device",
            kind="tailscale.device",
            spec={"connection_ref": "", "name": "a-box", "key_expiry_disabled": False},
        )
        self.resource.conditions = [
            {
                "type": "Ready",
                "status": True,
                "reason": "Observed",
                "message": "Matches HQ's config.",
            }
        ]
        self.resource.save(update_fields=["conditions"])

    def _sweep(self, *, key_expiry_disabled):
        # A sweep reports the expiry date, and its *absence* is the setting,
        # see `_tailnet_device_from_record`. Built from the record shape rather
        # than from the spec shape, so this exercises the same mapping the real
        # sweep goes through.
        confirm_observed(
            {
                "tailscale.device": {
                    "ok": True,
                    "records": [
                        {
                            "name": "a-box",
                            "key_expires": ("" if key_expiry_disabled else "2027-01-01T00:00:00Z"),
                            "tags": [],
                        }
                    ],
                }
            }
        )
        self.resource.refresh_from_db()

    def test_the_message_names_the_field_and_both_values(self):
        self._sweep(key_expiry_disabled=True)

        self.assertIn(
            "Disable key expiry is now True. HQ set it to False.",
            self.resource.conditions[0]["message"],
        )

    def test_the_summary_card_reports_the_drift_too(self):
        """A condition nothing reads is a condition that was not written.

        Asserted through `resource_health`, because that is what the page shows
        above the conditions table, and a false `Ready` is not the opposite of
        a true one, it is a row that surface skips entirely.
        """

        from hq.platform.application.infrastructure import resource_health

        self._sweep(key_expiry_disabled=True)

        self.assertEqual(resource_health(self.resource)["state"], "drifted")

    def test_a_matching_record_is_still_confirmed(self):
        self._sweep(key_expiry_disabled=False)

        self.assertIsNotNone(self.resource.last_observed_at)
        self.assertEqual(self.resource.conditions[0]["reason"], "Observed")


class UnobservableFieldTests(TestCase):
    """A blank the provider could never fill is not a disagreement.

    NPM answers with a certificate id, not an HQ resource key, so
    `_proxy_from_record` blanks `certificate_resource`. Compared as a value,
    every proxy host that named a certificate differed from its declaration
    forever, and `confirm_observed` rightly refuses to call drift observed, so
    those resources kept the condition their last reconcile wrote and no sweep
    ever confirmed them again. They read healthy the whole time.
    """

    def setUp(self):
        self.principal = cli_principal()
        self.resource = ManagedResource.objects.create(
            key="secured-proxy",
            kind="npm.proxy_host",
            spec={**PROVIDERS["npm.proxy_host"].from_record(A_PROXY), "certificate_resource": "example-wildcard"},
        )

    def sweep(self, record=None):
        record_sweep(a_sweep(**{"npm.proxy_host": [record or A_PROXY]}), principal=self.principal)
        self.resource.refresh_from_db()

    def test_a_host_naming_a_certificate_is_confirmed_by_a_sweep_that_cannot_see_it(self):
        self.assertIsNone(self.resource.last_observed_at)
        self.sweep()
        self.assertIsNotNone(self.resource.last_observed_at)
        self.assertEqual(self.resource.conditions[0]["reason"], "Observed")
        self.assertEqual(self.resource.spec["certificate_resource"], "example-wildcard")

    def test_drift_in_an_observable_field_is_still_refused(self):
        """Only unobservable fields are exempt, not the whole record."""
        self.sweep({**A_PROXY, "forward_port": 9999})
        self.assertIsNone(self.resource.last_observed_at)

    def test_every_unobservable_field_names_a_real_field_of_its_spec(self):
        for kind, provider in PROVIDERS.items():
            for field in provider.unobservable_fields:
                with self.subTest(kind=kind, field=field):
                    self.assertIn(field, provider.spec_type.model_fields)

    def test_every_adoptable_provider_supplies_a_record_to_check(self):
        """The guard must cover the registry, not a list kept by hand, so every
        adoptable provider is checked.
        """
        adoptable = {k for k, p in PROVIDERS.items() if p.from_record}
        sampled = {k for k, p in PROVIDERS.items() if p.sample_record}
        self.assertEqual(adoptable - sampled, set())

    def test_a_blanked_field_is_declared_unobservable_and_a_declared_one_is_really_blanked(self):
        """Exemptions accrete, and each one forgives drift forever."""
        for kind, provider in _adoptable():
            rebuilt = provider.from_record(provider.sample_record)
            for field, value in rebuilt.items():
                if value != "" or field not in provider.spec_type.model_fields:
                    continue
                if provider.sample_record.get(field, "") == "":
                    continue  # the record itself was blank; nothing was invented
                with self.subTest(kind=kind, field=field):
                    self.assertIn(
                        field,
                        provider.unobservable_fields,
                        f"{kind}.{field} is blanked by from_record but not declared "
                        "unobservable, so a declaration naming it is never confirmed",
                    )
            for field in provider.unobservable_fields:
                supplied = provider.sample_record.get(field, "")
                if supplied == "":
                    continue
                with self.subTest(kind=kind, field=field, exemption=True):
                    self.assertNotEqual(str(rebuilt.get(field, "")), str(supplied))

    def test_a_field_the_reading_never_carries_is_declared_even_when_omitted(self):
        """The guard above skips fields its sample record omits, which is
        exactly what a field the reading never carries looks like.

        Asked behaviorally instead: set the field, sweep the record it was
        built from, and nothing should be left unconfirmed. Booleans are
        covered as well as strings: a flag is exactly the kind of field HQ
        keeps for itself (fold this row away, keep this device on the tailnet),
        so skipping the type would skip the likeliest case.
        """

        from ..topology import _unconfirmed

        # What to set the field to, per type, so that it asserts something. A
        # `False` would assert nothing and be excluded from the comparison, and
        # the guard would pass by not having tried.
        assertions = {str: "held-by-hq", bool: True}
        for kind, provider in _adoptable():
            declared = set(provider.unobservable_fields or ())
            absent = set(provider.spec_type.model_fields) - set(provider.sample_record)
            for field in sorted(absent - declared):
                # Typed from the model, not the built spec: `from_record`
                # never emits these, so the result has no type to read.
                annotation = provider.spec_type.model_fields[field].annotation
                if annotation not in assertions:
                    continue
                spec = provider.from_record(provider.sample_record)
                spec[field] = assertions[annotation]
                with self.subTest(kind=kind, field=field):
                    resource = ManagedResource.objects.create(
                        key=f"unconfirmed-{kind.replace('.', '-')}-{field}",
                        kind=kind,
                        spec=spec,
                    )
                    record_sweep(
                        a_sweep(**{kind: [provider.sample_record]}),
                        principal=cli_principal(),
                    )
                    resource.refresh_from_db()
                    self.assertEqual(
                        _unconfirmed(resource, provider),
                        (),
                        f"{kind}.{field} is asserted by a declaration and never "
                        "read back, so it raises a finding no sweep or reconcile "
                        "can clear. Declare it in unobservable_fields.",
                    )

    def test_a_swept_record_confirms_the_declaration_it_matches(self):
        """The invariant, asked of every adoptable provider.

        This is the test that fails the day any provider acquires a blanked
        field, whether or not anyone thought about that field.
        """
        for kind, provider in _adoptable():
            with self.subTest(kind=kind):
                spec = provider.from_record(provider.sample_record)
                resource = ManagedResource.objects.create(key=f"sample-{kind.replace('.', '-')}", kind=kind, spec=spec)
                record_sweep(a_sweep(**{kind: [provider.sample_record]}), principal=cli_principal())
                resource.refresh_from_db()
                self.assertIsNotNone(
                    resource.last_observed_at,
                    f"a {kind} declaration was not confirmed by a sweep of the very record it was built from",
                )
                self.assertEqual(resource.conditions[0]["reason"], "Observed")


class LineEndingsAreNotDriftTests(TestCase):
    """A document saved through a form is the same document the API returns.

    HTML submits a textarea as CRLF and every provider returns LF, so the two
    differ byte for byte while saying exactly the same thing. Compared raw, a
    policy reads as drifted seconds after a reconcile the provider accepted.
    """

    DOCUMENT = '{\n  "grants": [],\n  "groups": {}\n}'

    def test_layout_is_not_drift_and_a_real_change_is(self):
        """CRLF against LF, one line against hundreds (HQ stores the policy it
        applied minified; Tailscale returns it pretty-printed) and object key
        order are layout. A list's order is not: in a policy the order of grants
        is the policy. The normalization must not swallow a difference that
        matters."""

        import json

        from ..inventory import _differences

        crlf = self.DOCUMENT.replace("\n", "\r\n")
        minified = json.dumps(json.loads(self.DOCUMENT), separators=(",", ":"))
        keyed = '{"groups":{},"grants":[{"a":1},{"b":2}]}'
        cases = (
            ("crlf and lf", crlf, self.DOCUMENT, False),
            ("minified and pretty", minified, self.DOCUMENT, False),
            ("key order", keyed, '{"grants":[{"a":1},{"b":2}],"groups":{}}', False),
            ("list order", keyed, '{"groups":{},"grants":[{"b":2},{"a":1}]}', True),
            ("a real change", crlf, self.DOCUMENT.replace("grants", "grant"), True),
        )
        for name, declared, found, drifted in cases:
            with self.subTest(name):
                differences = _differences("tailscale.policy", {"document": declared}, {"document": found})
                self.assertEqual(bool(differences), drifted)

    def test_a_document_that_is_not_json_is_still_compared_as_text(self):
        """A policy kept as HuJSON, with comments, does not parse. It falls back
        to the text comparison, which errs towards reporting a difference."""

        from ..inventory import _differences

        declared = {"document": '{\n  // admins\n  "grants": []\n}'}
        found = {"document": '{"grants":[]}'}

        self.assertNotEqual(_differences("tailscale.policy", declared, found), ())

    def test_a_spec_saved_with_crlf_is_stored_with_one_newline(self):
        """Normalized on the way in as well, so the stored value is comparable
        to anything, not only to what this one comparison normalizes.
        """

        spec = validate_spec(
            "tailscale.policy",
            {"connection_ref": "", "document": self.DOCUMENT.replace("\n", "\r\n")},
        )

        self.assertNotIn("\r", spec["document"])
        self.assertEqual(spec["document"], self.DOCUMENT)


class NothingIsJudgedAgainstAReadingThatDoesNotExistTests(TestCase):
    """A spec and a reading are two vocabularies, and some never overlap.

    A certificate declares what was asked for (which name, which domains, where
    to install) and its reading reports what exists: issuer, expiry, the PEM. A
    machine's only reading is telemetry. Compared by field name every declared
    field is unconfirmed forever, and no sweep or reconcile can clear it. The
    test is `from_record`: without one there is no way to turn a reading into a
    spec, so there is nothing to compare and nothing to report.
    """

    class _Resource:
        def __init__(self, spec, status):
            self.spec = spec
            self.status = status
            self.last_observed_at = timezone.now()

    def _unconfirmed(self, kind, status):
        from ..topology import _unconfirmed

        provider = PROVIDERS[kind]
        spec = dict.fromkeys(provider.spec_type.model_fields, "declared")
        return _unconfirmed(self._Resource(spec, status), provider)

    def test_a_provider_with_no_reading_reports_nothing_unconfirmed(self):
        for kind, provider in sorted(PROVIDERS.items()):
            if provider.from_record:
                continue
            with self.subTest(kind=kind):
                self.assertEqual(
                    self._unconfirmed(kind, {"something": "else"}),
                    (),
                    f"{kind} has no from_record, so every declared field would "
                    "report unconfirmed forever with nothing able to clear it",
                )

    def test_a_provider_with_a_reading_is_still_judged(self):
        """The exemption must not quietly turn drift detection off."""

        unconfirmed = self._unconfirmed("tailscale.device", {"name": "declared"})
        self.assertTrue(
            unconfirmed,
            "a provider that can be read back must still report what its reading did not confirm",
        )
        self.assertNotIn("name", unconfirmed)


class AcceptObservedTests(TestCase):
    """Keeping a change made at the provider, instead of reconciling it away."""

    def setUp(self):
        from ..inventory_testing import store

        self.resource = ManagedResource.objects.create(
            key="a-device",
            kind="tailscale.device",
            spec={"connection_ref": "", "name": "a-box", "key_expiry_disabled": False},
        )
        # The provider says the key never expires: it was changed there.
        store("tailscale.device", {"name": "a-box", "key_expires": "", "tags": []})

    def accept(self, principal=None):
        from ..resource_operations import OperationCommand, accept_observed

        return accept_observed(
            OperationCommand(idempotency_key="accept-1", reason="changed on purpose"),
            principal=principal or cli_principal(),
            current_key="a-device",
        )

    def test_the_declaration_takes_what_the_provider_holds(self):
        self.accept()

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.spec["key_expiry_disabled"])
        self.assertTrue(AuditLog.objects.filter(object_repr__contains="a-device").exists())

    def test_nothing_seen_is_nothing_to_accept(self):
        ProviderInventory.objects.filter(kind="tailscale.device").delete()

        with self.assertRaises(RecordNotFound):
            self.accept()


class DriftPhraseTests(TestCase):
    """A drift is said so a person can see what changed, not pasted whole."""

    def test_a_short_value_is_quoted(self):
        from ..inventory import _difference_phrase

        self.assertEqual(
            _difference_phrase("answer", "192.0.2.1", "192.0.2.2"),
            "answer is now 192.0.2.2. HQ set it to 192.0.2.1.",
        )

    def test_two_documents_are_compared_by_what_they_say(self):
        import json

        from ..inventory import _difference_phrase

        declared = {"grants": [{"src": ["a"]}] * 20, "groups": {"group:empty": []}, "hosts": {"x": "192.0.2.1"}}
        live = {**declared, "autoApprovers": {"services": {}}, "grants": declared["grants"] + [{"src": ["b"]}]}
        del live["groups"]

        phrase = _difference_phrase("document", json.dumps(declared), json.dumps(live))

        self.assertEqual(phrase, "document differs from HQ's copy: + autoApprovers, - groups, grants changed.")

    def test_a_long_value_that_is_not_a_document_is_sized_not_pasted(self):
        from ..inventory import _difference_phrase

        phrase = _difference_phrase("note", "a" * 200, "b" * 300)

        self.assertNotIn("aaaa", phrase)
        self.assertIn("300 characters live", phrase)
