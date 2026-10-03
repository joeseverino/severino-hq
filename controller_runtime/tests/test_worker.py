"""Tests for the worker: one run, its entry point, and what it reports."""

from __future__ import annotations

import io
import json
import os
import subprocess
from unittest import TestCase, mock

from .. import providers, worker
from controller_runtime import connection_env, portainer
from control_plane.provider_adapters.contracts import ProviderError, ProviderResult
from .test_support import _bridge


class WorkerTests(TestCase):
    def setUp(self):
        # A pass prints its JSON result on stdout, which is the worker's answer
        # to systemd, not something these tests read. Captured, so a line in
        # the suite's output is always something worth looking at.
        self.enterContext(mock.patch("sys.stdout", new_callable=io.StringIO))

    @mock.patch.dict("os.environ", {"HQ_IN_PROCESS": "1"}, clear=True)
    @mock.patch("controller_runtime.worker.subprocess.run")
    def test_in_process_bridge_uses_running_image_python(self, run):
        run.return_value = mock.Mock(
            returncode=0,
            stdout='{"ok":true,"operation":null}',
        )

        worker._manage("peek")

        command = run.call_args.args[0]
        self.assertEqual(command[0], worker.sys.executable)
        self.assertTrue(command[1].endswith("/manage.py"))
        self.assertNotIn("docker", command)

    @mock.patch.dict("os.environ", {"HQ_IN_PROCESS": "1"}, clear=True)
    @mock.patch("controller_runtime.worker.subprocess.run")
    def test_a_payload_goes_on_standard_input_whatever_its_size(self, run):
        """One argument is capped at 128 KiB; a whole sweep is larger."""

        run.return_value = mock.Mock(returncode=0, stdout='{"ok":true}')
        sweep = {"example.kind": {"ok": True, "records": [{"name": "x" * 1000}] * 300}}

        worker._manage("inventory", "--controller-id", "test", payload=sweep)

        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["--payload", "-"])
        self.assertLess(max(len(part) for part in command), 4096)
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), sweep)

    @mock.patch.dict("os.environ", {"HQ_IN_PROCESS": "1"}, clear=True)
    @mock.patch("controller_runtime.worker.subprocess.run")
    def test_a_failed_bridge_says_why(self, run):
        run.return_value = mock.Mock(
            returncode=1, stdout="", stderr="Traceback\nCommandError: Unknown kind.\n"
        )

        with self.assertRaisesRegex(worker.BridgeError, "Unknown kind"):
            worker._manage("peek")

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker._manage")
    def test_idle_plan_reports_connections_without_claiming(self, manage, connections):
        manage.return_value = {"ok": True, "operation": None}

        self.assertEqual(worker.run_once("test", apply=False), 0)

        self.assertEqual(manage.call_args.args[0], "peek")
        self.assertNotIn("claim", manage.call_args.args)
        connections.assert_called_once_with()

    @mock.patch("builtins.print")
    @mock.patch(
        "controller_runtime.worker.connections",
        return_value=[{"connection_ref": "broken", "ok": False}],
    )
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_plan_reports_broken_connections_and_returns_failure(
        self, manage, execute, connections, output
    ):
        manage.return_value = {
            "operation": {"id": "operation-1", "action": "reconcile"},
            "resource": {"key": "dns", "kind": "adguard.rewrite", "spec": {}},
        }
        execute.return_value = ProviderResult(
            changed=False, status={}, conditions=[], message="Current."
        )

        self.assertEqual(worker.run_once("test", apply=False), 1)

        payload = json.loads(output.call_args.args[0])
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["connections"][0]["ok"])
        connections.assert_called_once_with()
        execute.assert_called_once()

    def plan(self, *rounds):
        """A plan run whose probes answer ``rounds`` in turn; its exit and output."""

        with mock.patch("builtins.print") as output, \
                mock.patch("controller_runtime.worker.connections", side_effect=list(rounds)) as probes, \
                mock.patch("controller_runtime.worker._manage", return_value={"operation": None}):
            code = worker.run_once("test", apply=False)
        return code, json.loads(output.call_args.args[0]), probes.call_count

    def test_a_provider_that_did_not_answer_once_is_asked_again(self):
        blip = {"connection_ref": "example-tailnet", "ok": False, "detail": "no answer", "failure": "network"}
        good = {"connection_ref": "example-dns", "ok": True, "detail": "ok"}

        code, payload, asked = self.plan([blip, good], [{**blip, "ok": True, "detail": "ok"}, good])

        self.assertEqual((code, asked), (0, 2))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["warnings"], [])

    def test_one_provider_the_network_keeps_failing_warns_and_does_not_roll_back(self):
        blip = {"connection_ref": "example-tailnet", "ok": False, "detail": "no answer", "failure": "network"}
        good = {"connection_ref": "example-dns", "ok": True, "detail": "ok"}

        code, payload, _ = self.plan([blip, good], [blip, good])

        self.assertEqual(code, 0)
        self.assertEqual(payload["warnings"], ["example-tailnet: no answer"])

    def test_every_provider_failing_on_the_network_is_this_image_and_fails(self):
        down = [{"connection_ref": ref, "ok": False, "detail": "no answer", "failure": "network"} for ref in ("a", "b")]

        code, payload, _ = self.plan(down, down)

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    def test_a_refused_credential_still_fails_the_plan(self):
        refused = {"connection_ref": "example-dns", "ok": False, "detail": "refused", "failure": "credential"}
        good = {"connection_ref": "example-tailnet", "ok": True, "detail": "ok"}

        code, _, asked = self.plan([refused, good])

        self.assertEqual((code, asked), (1, 1))

    @mock.patch("controller_runtime.worker.analytics_sites")
    @mock.patch("controller_runtime.worker.inventory", return_value={})
    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker._manage")
    def test_a_kind_hq_forces_is_read_when_nothing_else_is_due(
        self, manage, connections, inventory, sites
    ):
        manage.side_effect = _bridge(
            **{
                "sweep-due": {
                    "ok": True,
                    "due": True,
                    "carry": ["example-ssh"],
                    "only_kinds": ["tailscale.device"],
                },
                "connections": {"ok": True},
                "inventory": {"ok": True},
            }
        )

        worker._report_findings("test")

        inventory.assert_called_once_with(only=frozenset({"tailscale.device"}))
        connections.assert_called_once_with(carry=frozenset({"example-ssh"}))
        # Only what was asked for: no analytics read rides along.
        sites.assert_not_called()

    def test_inventory_reads_only_the_kinds_named(self):
        read = mock.Mock(return_value=[])
        unread = mock.Mock(return_value=[])
        with (
            mock.patch.dict(
                providers.PROVIDER_INVENTORY,
                {"example.read": read, "example.unread": unread},
                clear=True,
            ),
            mock.patch.object(providers, "_has_source", return_value=True),
            mock.patch.object(connection_env, "connection_prefixes", return_value={}),
        ):
            found = providers.inventory(only=frozenset({"example.read"}))

        self.assertEqual(set(found), {"example.read"})
        read.assert_called_once_with()
        unread.assert_not_called()

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker._manage")
    def test_idle_apply_asks_for_queued_work_before_and_after_the_sweep(self, manage, _connections):
        manage.side_effect = _bridge(
            **{
                "sweep-due": {"ok": True, "due": True},
                "connections": {"ok": True},
                "inventory": {"ok": True},
                "analytics": {"ok": True},
                "schedule": {"ok": True},
                "claim": {"ok": True, "operation": None},
            }
        )

        self.assertEqual(worker.run_once("test", apply=True), 0)

        called = [call.args[0] for call in manage.call_args_list]
        # What somebody queued is asked for before the sweep, so it never waits
        # behind every reader; what the sweep found to do is asked for after
        # it. Within the sweep, what HQ can reach is reported ahead of what it
        # found there, so an empty inventory can be read against the
        # credential that would have filled it.
        self.assertEqual(
            called,
            [
                "glance-plan",
                "claim",
                "sweep-due",
                "connections",
                "inventory",
                "analytics",
                "schedule",
                "claim",
            ],
        )
        arguments = manage.call_args.args
        self.assertEqual(arguments[:3], ("claim", "--controller-id", "test"))
        self.assertIn("adguard.rewrite:reconcile", arguments)
        self.assertIn("npm.proxy_host:reconcile", arguments)
        self.assertIn("tls.certificate:reconcile", arguments)
        self.assertIn("tls.certificate:renew", arguments)

    @mock.patch("controller_runtime.worker.inventory", return_value={"records": []})
    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.analytics")
    @mock.patch("controller_runtime.worker.analytics_sites")
    @mock.patch("controller_runtime.worker._manage")
    def test_hq_plans_each_sites_missing_analytics_before_it_is_read(
        self, manage, sites, analytics, _connections, _inventory
    ):
        site = {
            "account": "provider-only-account-id",
            "connection_ref": "example-api",
            "site_tag": "0" * 32,
            "host": "example.test",
        }
        window = {
            "connection_ref": "example-api",
            "site_tag": "0" * 32,
            "start": "2026-06-01",
            "end": "2026-08-28",
            "reason": "backfill",
        }
        sites.return_value = [site]
        analytics.return_value = {"sites": []}
        manage.side_effect = _bridge(
            **{
                "sweep-due": {"ok": True, "due": True},
                "analytics-plan": {"ok": True, "windows": [window]},
                "connections": {"ok": True},
                "inventory": {"ok": True},
                "analytics": {"ok": True},
            }
        )

        worker._report_findings("test")

        analytics.assert_called_once_with(sites=[site], windows=[window])
        plan_call = next(
            call for call in manage.call_args_list if call.args[0] == "analytics-plan"
        )
        self.assertNotIn("provider-only-account-id", " ".join(plan_call.args))

    def _queued(self, manage, *keys, due=True):
        """A bridge whose queue holds one reconcile per key, claimed in order."""

        waiting = [
            {
                "operation": {"id": f"operation-{key}", "action": "reconcile"},
                "resource": {"key": key, "kind": "adguard.rewrite", "generation": 2, "spec": {}},
            }
            for key in keys
        ]
        answer = _bridge(
            **{
                "sweep-due": {"ok": True, "due": due},
                "connections": {"ok": True, "recorded": []},
                "inventory": {"ok": True, "recorded": []},
                "analytics": {"ok": True, "recorded": {}},
                "schedule": {"ok": True, "scheduled": []},
                "report": {"ok": True},
            }
        )

        def respond(*args, **kwargs):
            if args[0] == "claim":
                return waiting.pop(0) if waiting else {"ok": True, "operation": None}
            return answer(*args, **kwargs)

        manage.side_effect = respond

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_queued_work_is_applied_before_the_sweep_it_used_to_wait_behind(
        self, manage, execute, _connections
    ):
        self._queued(manage, "dns")
        execute.return_value = ProviderResult(changed=True, status={}, conditions=[], message="Applied.")

        self.assertEqual(worker.run_once("test", apply=True), 0)

        called = [call.args[0] for call in manage.call_args_list]
        self.assertLess(called.index("report"), called.index("sweep-due"))
        # The sweep still runs, and reads the estate as the work left it.
        self.assertIn("inventory", called)

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_several_queued_together_are_applied_in_one_run(self, manage, execute, _connections):
        self._queued(manage, "one", "two", "three")
        execute.return_value = ProviderResult(changed=True, status={}, conditions=[], message="Applied.")

        self.assertEqual(worker.run_once("test", apply=True), 0)

        self.assertEqual([call.args[0]["key"] for call in execute.call_args_list], ["one", "two", "three"])

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_a_run_applies_a_bounded_number_and_leaves_the_rest(self, manage, execute, _connections):
        # More than both passes together could take, so only the bound stops it.
        self._queued(manage, *(f"r{index}" for index in range(2 * worker.APPLY_LIMIT + 3)), due=False)
        execute.return_value = ProviderResult(changed=True, status={}, conditions=[], message="Applied.")

        self.assertEqual(worker.run_once("test", apply=True), 0)

        # Once before the sweep and once after it, each exactly to the limit.
        self.assertEqual(execute.call_count, 2 * worker.APPLY_LIMIT)

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_work_that_breaks_the_run_does_not_stop_the_estate_being_looked_at(
        self, manage, execute, _connections
    ):
        """HQ refusing a report, or a handler that cannot run, ends the run as
        it always did, but only after the sweep: before queued work went
        first, the sweep had already happened by the time anything could break."""

        self._queued(manage, "dns")
        execute.side_effect = KeyError("a handler that could not run")

        with self.assertRaises(KeyError):
            worker.run_once("test", apply=True)

        called = [call.args[0] for call in manage.call_args_list]
        self.assertIn("inventory", called)
        self.assertIn("schedule", called)
        # And nothing more is claimed on top of what broke.
        self.assertEqual(called.count("claim"), 1)

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_a_failure_stops_the_queue_and_still_lets_the_sweep_run(self, manage, execute, _connections):
        self._queued(manage, "one", "two")
        execute.side_effect = ProviderError("Provider request failed.")

        self.assertEqual(worker.run_once("test", apply=True), 1)

        called = [call.args[0] for call in manage.call_args_list]
        # The one that failed is reported; the one behind it is not started on
        # top of a step that did not happen, in this run at all.
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(called.count("claim"), 1)
        # A failing operation does not leave the estate unwatched.
        self.assertIn("inventory", called)
        self.assertIn("schedule", called)

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_when_the_work_and_the_sweep_both_break_neither_is_lost(self, manage, execute, _connections):
        self._queued(manage, "dns")
        execute.side_effect = KeyError("a handler that could not run")
        answer = manage.side_effect

        def respond(*args, **kwargs):
            if args[0] == "schedule":
                raise worker.BridgeError("HQ did not answer")
            return answer(*args, **kwargs)

        manage.side_effect = respond

        with self.assertRaises(KeyError) as raised:
            worker.run_once("test", apply=True)

        # The first thing that went wrong is what the run ends with, and what
        # went wrong after it travels with it.
        self.assertIsInstance(raised.exception.__context__, worker.BridgeError)

    def test_capability_registry_drives_supported_kinds(self):
        """What the controller offers is the registry, minus what is locked.

        Derived rather than listed: a provider added to the registry appears
        here without this test being edited, which is the whole claim the
        registry makes.
        """
        from control_plane.providers import controller_capability_registry

        expected = tuple(
            sorted(
                (kind, action)
                for kind, capability in (
                    controller_capability_registry().capabilities.items()
                )
                for action, settings in capability.actions.items()
                # A locked action needs a credential the controller does not
                # hold (a zone's own settings, for one) so it is declared
                # and never offered.
                if settings.mode != "locked"
            )
        )

        self.assertEqual(worker.supported_capabilities(), expected)
        # And the exclusion is real, not vacuous.
        self.assertNotIn(("cloudflare.zone", "reconcile"), expected)

    def test_removal_is_never_scheduled_automatically(self):
        """The scheduler converges declarations. It must not decide to delete.

        Reconciliation moves the world toward what HQ says and is safe to run
        unattended. Removal takes down something that is currently serving, and
        an automatic one would mean a bad declaration could delete a live record
        with nobody having asked for it.
        """
        from control_plane.providers import enabled_controller_actions

        automatic = enabled_controller_actions(automatic_only=True)

        self.assertEqual([a for _, a in automatic if a == "delete"], [])

    def test_every_declared_controller_action_has_exactly_one_dispatch(self):
        from control_plane.providers import controller_capability_registry

        declared = {
            (kind, action)
            for kind, capability in controller_capability_registry().capabilities.items()
            for action in capability.actions
        }

        self.assertEqual(set(providers.PROVIDER_ACTIONS), declared)

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_provider_failure_is_reported_without_secret(
        self, manage, execute, _connections
    ):
        self._queued(manage, "dns")
        execute.side_effect = ProviderError("Provider request failed.")

        self.assertEqual(worker.run_once("test", apply=True), 1)

        report = next(call for call in manage.call_args_list if call.args[0] == "report")
        report_payload = report.kwargs["payload"]
        self.assertFalse(report_payload["success"])
        self.assertNotIn("password", json.dumps(report_payload).lower())

    @mock.patch("controller_runtime.worker.connections", return_value=[])
    @mock.patch("controller_runtime.worker.execute")
    @mock.patch("controller_runtime.worker._manage")
    def test_claimed_operation_executes_without_an_unrelated_global_gate(
        self, manage, execute, connections
    ):
        self._queued(manage, "dns")
        execute.return_value = ProviderResult(
            changed=False,
            status={},
            conditions=[],
            message="Current.",
        )

        self.assertEqual(worker.run_once("test", apply=True), 0)

        connections.assert_called_once_with(carry=frozenset())
        execute.assert_called_once()


class ControllerStepReportingTests(TestCase):
    """A failure should name the step that failed, not the module that ran it."""

    def test_a_failing_step_is_named(self):
        from controller_runtime.providers import ProviderError
        from controller_runtime.commands import run_command

        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout=b"", stderr=b"boom")
            with self.assertRaises(ProviderError) as caught:
                run_command(["/bin/false"], step="SSH preflight for somewhere")
        self.assertIn("SSH preflight for somewhere", str(caught.exception))
        self.assertNotIn("Certificate", str(caught.exception))

    def test_a_step_whose_tool_is_missing_says_so_in_the_log(self):
        """A tool that is not on the machine raises before there is an exit code.

        The provider result says only that the step could not complete, so the
        log is the only place the cause can be read. The exception's type is
        that cause, and it belongs in the message: the controller's formatter
        prints the message and drops everything else.
        """

        from controller_runtime.providers import ProviderError

        from controller_runtime.commands import run_command

        with mock.patch("subprocess.run") as run:
            run.side_effect = FileNotFoundError(2, "No such file or directory", "op")
            with self.assertLogs("severino.controller", level="WARNING") as logged:
                with self.assertRaises(ProviderError) as caught:
                    run_command(["op", "item", "get"], step="1Password read for a target")

        self.assertIn("1Password read for a target", logged.output[0])
        self.assertIn("FileNotFoundError", logged.output[0])
        self.assertIn("1Password read for a target", str(caught.exception))

    def test_a_step_that_never_returned_says_so_in_the_log(self):
        from controller_runtime.providers import ProviderError
        from controller_runtime.commands import run_command

        with mock.patch("subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd=["op"], timeout=180)
            with self.assertLogs("severino.controller", level="WARNING") as logged:
                with self.assertRaises(ProviderError):
                    run_command(["op", "item", "get"], step="1Password read for a target")

        self.assertIn("TimeoutExpired", logged.output[0])

    def test_a_credential_is_struck_out_of_the_silent_branch_too(self):
        """A tool that names its credential while failing to start."""

        from controller_runtime.providers import ProviderError

        from controller_runtime.commands import run_command

        with mock.patch("subprocess.run") as run:
            run.side_effect = OSError("cannot run with token ops_secret_value")
            with self.assertLogs("severino.controller", level="WARNING") as logged:
                with self.assertRaises(ProviderError):
                    run_command(
                        ["op"],
                        step="a step",
                        env={"OP_SERVICE_ACCOUNT_TOKEN": "ops_secret_value"},
                    )

        # The detail rides in `extra`, which the formatted output drops, so the
        # record itself is what has to be checked: asserting on the rendered
        # line would pass for a value that was never redacted at all.
        detail = logged.records[0].detail
        self.assertIn("[redacted]", detail)
        self.assertNotIn("ops_secret_value", detail)
        self.assertNotIn("ops_secret_value", "".join(logged.output))

    def test_subprocess_output_never_reaches_the_result(self):
        """Remote paths and messages belong in the log, not in a provider result."""

        from controller_runtime.providers import ProviderError

        from controller_runtime.commands import run_command

        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=1, stdout=b"", stderr=b"/home/someone/secret/path missing"
            )
            with self.assertRaises(ProviderError) as caught:
                run_command(["/bin/false"], step="a step")
        self.assertNotIn("/home/someone", str(caught.exception))

    def test_what_the_tool_said_is_in_the_journal_message_itself(self):
        """The plain formatter drops `extra`, so the reason has to be in the line."""

        from controller_runtime.providers import ProviderError

        from controller_runtime.commands import run_command

        with (
            mock.patch("subprocess.run") as run,
            self.assertLogs("severino.controller", level="WARNING") as logged,
        ):
            run.return_value = mock.Mock(
                returncode=1,
                stdout=b"",
                stderr=b"noise\nPermissionError: Operation not permitted: 'a/key.pem'\n",
            )
            with self.assertRaises(ProviderError) as caught:
                run_command(["/bin/false"], step="certbot certonly")
        self.assertIn("certbot certonly (exit 1): PermissionError", logged.output[0])
        self.assertNotIn("noise", logged.output[0])
        self.assertEqual(str(caught.exception), "certbot certonly failed.")


class WorkerEntryPointTests(TestCase):
    """The worker's own start-up, which nothing else exercises.

    Every other test calls `run_once` directly and never builds the parser.
    """

    def test_it_starts_and_names_the_machine_it_runs_on(self):
        with (
            mock.patch.object(worker.sys, "argv", ["worker"]),
            mock.patch.object(worker, "run_once", return_value=0) as run,
        ):
            self.assertEqual(worker.main(), 0)

        self.assertEqual(run.call_args.args[0], os.uname().nodename)
        self.assertFalse(run.call_args.kwargs["apply"])

    @mock.patch.dict("os.environ", {"HQ_CONTROLLER_ID": "a-named-controller"})
    def test_the_environment_names_it_when_it_says_so(self):
        with (
            mock.patch.object(worker.sys, "argv", ["worker"]),
            mock.patch.object(worker, "run_once", return_value=0) as run,
        ):
            worker.main()

        self.assertEqual(run.call_args.args[0], "a-named-controller")

    def test_apply_is_off_unless_asked_for(self):
        # An applying pass reports its step failures on the way out, whatever
        # happened in it. Unpatched, that report would go over the real bridge
        # (a manage.py subprocess from inside a unit test) and pass only
        # because the failure is swallowed.
        with (
            mock.patch.object(worker.sys, "argv", ["worker", "--apply"]),
            mock.patch.object(worker, "run_once", return_value=0) as run,
            mock.patch.object(worker, "_post") as post,
        ):
            worker.main()

        self.assertTrue(run.call_args.kwargs["apply"])
        self.assertEqual(post.call_args.args[0], "steps")


class ThisRunIsNotTheEstateTests(TestCase):
    """The controller running the sweep is excluded by its per-run nonce only."""

    def listed(self, *containers, nonce="a-run-nonce"):
        with (
            mock.patch.dict(os.environ, {"HQ_CONTROLLER_RUN": nonce}),
            mock.patch.object(
                connection_env, "provider_connection_refs", return_value=("a-portainer",)
            ),
            mock.patch.object(
                portainer,
                "_portainer_environments",
                return_value=[
                    {
                        "id": 1,
                        "name": "a-docker-host",
                        "address": "10.0.0.9",
                        "local": False,
                        "reachable": True,
                    }
                ],
            ),
            mock.patch.object(portainer, "_portainer_stacks", return_value=[]),
            mock.patch.object(
                portainer, "_portainer_containers", return_value=list(containers)
            ),
        ):
            return [record["name"] for record in portainer._list_portainer_containers()]

    def test_the_controller_running_the_sweep_is_not_reported(self):
        self.assertEqual(
            self.listed(
                {"Names": ["/hq-controller"], "Labels": {"severino-hq.run": "a-run-nonce"}}
            ),
            [],
        )

    def test_a_role_label_does_not_hide_a_container(self):
        self.assertEqual(
            self.listed(
                {"Names": ["/a-spoof"], "Labels": {"severino-hq.role": "controller"}}
            ),
            ["a-spoof"],
        )

    def test_another_runs_nonce_does_not_hide_a_container(self):
        self.assertEqual(
            self.listed(
                {"Names": ["/a-spoof"], "Labels": {"severino-hq.run": "a-guess"}}
            ),
            ["a-spoof"],
        )

    def test_without_a_nonce_nothing_is_hidden(self):
        self.assertEqual(
            self.listed(
                {"Names": ["/a-spoof"], "Labels": {"severino-hq.run": ""}}, nonce=""
            ),
            ["a-spoof"],
        )

    def test_an_unlabelled_container_is_still_reported(self):
        self.assertEqual(
            self.listed(
                {"Names": ["/a-stray"]},
                {"Names": ["/a-web"], "Labels": {"com.docker.compose.project": "a-project"}},
                {"Names": ["/hq-controller"], "Labels": {"severino-hq.run": "a-run-nonce"}},
            ),
            ["a-stray", "a-web"],
        )


class SweepOrderTests(TestCase):
    """Providers are read at once; one provider's kinds, in turn."""

    def test_kinds_of_one_provider_never_overlap_and_providers_do(self):
        import threading
        import time

        lock = threading.Lock()
        running: dict[str, int] = {}
        most: dict[str, int] = {}
        together = 0

        def reader(provider):
            def read():
                nonlocal together
                with lock:
                    running[provider] = running.get(provider, 0) + 1
                    most[provider] = max(most.get(provider, 0), running[provider])
                    together = max(together, sum(running.values()))
                time.sleep(0.03)
                with lock:
                    running[provider] -= 1
                return []

            return read

        kinds = {f"{provider}.kind{index}": reader(provider) for provider in ("alpha", "beta", "gamma") for index in range(3)}
        with (
            mock.patch.dict(providers.PROVIDER_INVENTORY, kinds, clear=True),
            mock.patch.object(providers, "_has_source", return_value=True),
        ):
            found = providers.inventory()

        self.assertEqual(list(found), list(kinds))
        self.assertEqual(most, {"alpha": 1, "beta": 1, "gamma": 1})
        self.assertGreater(together, 1)


class ProviderGroupingTests(TestCase):
    """Which kinds are read in turn with which, against the real registries."""

    def test_one_vendors_kinds_are_one_group_whichever_credential_reads_them(self):
        # Read through two different credentials, and still one provider's:
        # its limits and its refusals are shared.
        self.assertEqual(providers._provider_of("cloudflare.zone"), "cloudflare")
        self.assertEqual(providers._provider_of("cloudflare.tunnel"), "cloudflare")
        self.assertEqual(providers._provider_of("github.repository"), "github")

    def test_every_kind_read_by_logging_in_is_one_group(self):
        """A host is never asked to open two sessions for one sweep."""

        self.assertEqual(providers._provider_of("host.perimeter"), "ssh")
        self.assertEqual(providers._provider_of("caddy.route"), "ssh")

    def test_every_kind_the_sweep_reads_has_a_group(self):
        for kind in providers.PROVIDER_INVENTORY:
            with self.subTest(kind=kind):
                self.assertTrue(providers._provider_of(kind))


class SlowSweepTests(TestCase):
    def test_a_long_sweep_names_its_slowest_readers(self):
        from controller_runtime import providers

        with self.assertLogs("severino.controller", level="WARNING") as logged:
            providers._say_if_slow({"example.fast": 1.0, "example.slow": 70.0, "example.middle": 5.0})

        self.assertIn("slowest: example.slow 70s, example.middle 5s, example.fast 1s", logged.output[0])

    def test_a_quick_sweep_says_nothing(self):
        from controller_runtime import providers

        with self.assertNoLogs("severino.controller", level="WARNING"):
            providers._say_if_slow({"example.fast": 1.0})
