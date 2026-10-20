"""HQ's checks on a commit: the review, the production report and the reads behind them."""

import json
import os
import subprocess
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, tag

from tools.pipeline import ci_report

ROOT = Path(__file__).resolve().parents[3]
REPO = "example-owner/example-repo"
SHA = "a" * 40
IMAGE = "ghcr.io/example-owner/example-repo/composition@sha256:" + "b" * 64
RUN = ci_report.Run("42", "https://github.com/example-owner/example-repo/actions/runs/42")


def job(id_, name, conclusion="success", steps=()):
    return {
        "id": id_,
        "name": name,
        "conclusion": conclusion,
        "started_at": "2026-10-02T00:00:00Z",
        "completed_at": "2026-10-02T00:01:05Z",
        "html_url": f"https://github.com/{REPO}/actions/runs/42/job/{id_}",
        "steps": list(steps),
    }


class FakeGh:
    """gh api, answering each path from a table; a listing comes in the pages given."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def __call__(self, args, env, stdin):
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        path = next(arg for arg in args[1:] if not arg.startswith("-") and arg not in {"POST", "PATCH"})
        self.calls.append({"method": method, "path": path, "args": list(args), "body": stdin and json.loads(stdin)})
        if method != "GET":
            return ""
        for prefix, answer in self.answers.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return json.dumps(answer)
        raise ci_report.GitHubError(f"HTTP 404: {path}")

    def writes(self):
        return [call for call in self.calls if call["method"] != "GET"]


def github(answers=None):
    fake = FakeGh(answers)
    return ci_report.GitHub("token", REPO, run=fake), fake


def jobs_listing(*pages):
    return {f"repos/{REPO}/actions/runs/42/jobs": [{"jobs": page} for page in pages]}


NO_CHECK = {f"repos/{REPO}/commits/{SHA}/check-runs": [{"check_runs": []}]}


@tag("source")
class ToolTests(SimpleTestCase):
    def test_it_runs_on_the_standard_library_alone(self):
        # -I -S: no site-packages, no environment, no working directory on the path.
        code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); import tools.pipeline.ci_report"
        done = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True, check=False)

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_log_on_stdin_is_diagnosed_in_the_catalogs_words(self):
        log = StringIO("secret: denied: permission_denied: write_package")
        with mock.patch("sys.stdin", log), redirect_stdout(StringIO()) as out:
            ci_report.main(["diagnose"])

        self.assertEqual(json.loads(out.getvalue())["id"], "package-write")
        self.assertNotIn("secret", out.getvalue())

    def test_the_image_does_not_carry_it(self):
        admitted = (ROOT / "Dockerfile.dockerignore").read_text().splitlines()

        self.assertIn("*", admitted)
        self.assertFalse([line for line in admitted if line.startswith("!tools")])


@tag("source")
class ListingTests(SimpleTestCase):
    def test_a_runs_jobs_are_read_from_every_page(self):
        gh, fake = github(
            {
                f"repos/{REPO}/actions/runs/42/attempts/2/jobs": [
                    {"jobs": [job(1, "Checks")]},
                    {"jobs": [job(2, "Compose / Build HQ", "failure", [{"name": "Sign", "conclusion": "failure"}])]},
                ]
            }
        )

        self.assertEqual(ci_report.failed_steps(gh, "42", "2", "Build HQ"), ["Sign"])
        self.assertIn("--paginate", fake.calls[0]["args"])

    def test_without_a_token_gh_reads_as_its_own_login(self):
        envs = []
        gh = ci_report.GitHub(None, REPO, run=lambda args, env, stdin: envs.append(env) or "[]")

        with mock.patch.dict("os.environ"):
            os.environ.pop("GH_TOKEN", None)
            gh.pages(f"repos/{REPO}/actions/runs")

        self.assertNotIn("GH_TOKEN", envs[0])

    def test_a_job_listing_cannot_be_read_as_one_page(self):
        gh, fake = github()

        with self.assertRaises(ValueError):
            gh.get(f"repos/{REPO}/actions/runs/42/jobs?per_page=100")
        self.assertEqual(fake.calls, [])


@tag("source")
class CheckTests(SimpleTestCase):
    verdict = ci_report.Verdict("", "Checking", "Running.")

    def test_the_check_in_progress_is_moved(self):
        gh, fake = github(
            {
                f"repos/{REPO}/commits/{SHA}/check-runs": [
                    {"check_runs": [{"id": 7, "status": "completed"}]},
                    {"check_runs": [{"id": 9, "status": "in_progress"}]},
                ]
            }
        )

        ci_report.post_check(gh, ci_report.REVIEW, SHA, "in_progress", self.verdict)

        (write,) = fake.writes()
        self.assertEqual((write["method"], write["path"]), ("PATCH", f"repos/{REPO}/check-runs/9"))
        self.assertIn("check_name=Severino%20HQ%20%C2%B7%20Review&", fake.calls[0]["path"])

    def test_a_finished_check_is_kept_and_a_new_one_opened(self):
        gh, fake = github(
            {f"repos/{REPO}/commits/{SHA}/check-runs": [{"check_runs": [{"id": 7, "status": "completed"}]}]}
        )

        ci_report.post_check(
            gh, ci_report.PRODUCTION, SHA, "completed", ci_report.Verdict("success", "Live", "Yes."), text="t", url="u"
        )

        (write,) = fake.writes()
        self.assertEqual((write["method"], write["path"]), ("POST", f"repos/{REPO}/check-runs"))
        self.assertEqual(write["body"]["conclusion"], "success")
        self.assertEqual(write["body"]["details_url"], "u")
        self.assertEqual(write["body"]["output"]["text"], "t")

    def test_what_was_not_given_is_not_sent(self):
        gh, fake = github(NO_CHECK)

        ci_report.post_check(gh, ci_report.REVIEW, SHA, "in_progress", self.verdict)

        body = fake.writes()[0]["body"]
        self.assertNotIn("conclusion", body)
        self.assertNotIn("details_url", body)
        self.assertNotIn("text", body["output"])

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(SystemExit), redirect_stdout(StringIO()), mock.patch("sys.stderr", StringIO()):
            ci_report.main(["check", ci_report.REVIEW, SHA, "done", "t", "s"])


@tag("source")
class VerdictTests(SimpleTestCase):
    def test_skipped_passes_unless_the_job_is_required(self):
        results = {"tests": {"result": "skipped"}, "image": {"result": "skipped"}, "scan": {"result": "cancelled"}}

        self.assertEqual(
            ci_report.unmet(results, ["image", "compose"]),
            ["image: skipped", "scan: cancelled", "compose: not run"],
        )

    def test_without_the_app_it_only_says_so(self):
        failing = {"RESULTS": json.dumps({"tests": {"result": "failure"}}), "APP_TOKEN": ""}
        passing = {"RESULTS": json.dumps({"tests": {"result": "success"}}), "APP_TOKEN": ""}
        for env, code in ((failing, 1), (passing, 0)):
            with self.subTest(code=code), mock.patch.dict("os.environ", env), redirect_stdout(StringIO()) as out:
                self.assertEqual(ci_report.main(["verdict"]), code)
                self.assertTrue(out.getvalue())

    def test_ready_lists_every_gate_in_pipeline_order_from_every_page(self):
        reader, _ = github(jobs_listing([job(3, "Image"), job(4, "Ready")], [job(1, "Checks"), job(2, "Tests (3.15)")]))
        app, fake = github(NO_CHECK)

        verdict = ci_report.review(reader, app, RUN, SHA, [], read=self.fail)

        self.assertEqual(verdict.title, "Ready to merge")
        body = fake.writes()[0]["body"]
        table = body["output"]["text"]
        self.assertEqual(body["name"], ci_report.REVIEW)
        self.assertLess(table.index("[Checks]"), table.index("[Tests (3.15)]"))
        self.assertLess(table.index("[Tests (3.15)]"), table.index("[Image]"))
        self.assertNotIn("[Ready]", table)
        self.assertIn("1m 5s", table)

    def test_held_names_what_failed_and_why_and_never_the_log(self):
        reader, _ = github(jobs_listing([job(1, "Checks"), job(2, "Image", "failure")]))
        app, fake = github(NO_CHECK)
        secret = "private-extension-name"

        verdict = ci_report.review(
            reader,
            app,
            RUN,
            SHA,
            ["image: failure"],
            read=lambda _: f"{secret}: denied: permission_denied: write_package",
        )

        self.assertEqual(verdict.conclusion, "failure")
        self.assertTrue(verdict.summary.startswith("Not passing: Image. "))
        self.assertIn(
            "package-write", [entry["id"] for entry in ci_report.diagnoses() if entry["title"] in verdict.title]
        )
        self.assertNotIn(secret, json.dumps(fake.writes()))

    def test_a_log_that_cannot_be_read_is_named_and_the_verdict_still_posted(self):
        reader, _ = github(jobs_listing([job(2, "Browser", "timed_out")]))
        app, fake = github(NO_CHECK)

        def unreadable(_):
            raise ci_report.LogUnreadable("HTTP 410 Gone")

        with mock.patch("sys.stderr", StringIO()) as err:
            verdict = ci_report.review(reader, app, RUN, SHA, ["browser: timed_out"], read=unreadable)

        self.assertIn("Browser: HTTP 410 Gone", err.getvalue())
        self.assertEqual(verdict.title, f"Held: {ci_report.UNKNOWN['title']}")
        self.assertEqual(len(fake.writes()), 1)

    def test_a_required_job_that_was_skipped_is_named_as_it_was_needed(self):
        reader, _ = github(jobs_listing([job(1, "Checks"), job(2, "Image", "skipped")]))
        app, _ = github(NO_CHECK)

        verdict = ci_report.review(reader, app, RUN, SHA, ["image: skipped"], read=self.fail)

        self.assertTrue(verdict.summary.startswith("Not passing: image. "))


def delivery(result):
    return ci_report.Delivery(commit=SHA, image=IMAGE, result=result, run=RUN, compose_run="41")


APPROVED = {f"repos/{REPO}/actions/runs/42/approvals": [[{"state": "approved", "user": {"login": "example-user"}}]]}


@tag("source")
class ReportTests(SimpleTestCase):
    def report(self, result, reads=None, answers=None, comments=()):
        reader, _ = github({**jobs_listing([job(5, "Deploy", result)]), **(answers or {})})
        app, fake = github(
            {
                **NO_CHECK,
                f"repos/{REPO}/commits/{SHA}/pulls": [[{"number": 12, "merged_at": "2026-10-02T00:00:00Z"}]],
                f"repos/{REPO}/issues/12/comments": [[{"body": body} for body in comments]],
            }
        )
        waits = []
        reads = list(reads or [])
        verdict = ci_report.report(reader, app, delivery(result), lambda _: reads.pop(0), lambda: waits.append(1))
        return verdict, fake.writes(), waits

    def test_live_is_said_on_the_commit_and_once_on_its_pull_request(self):
        verdict, writes, _ = self.report("success", answers=APPROVED)

        check, comment = writes
        self.assertEqual(verdict.title, "Live in production")
        self.assertEqual(check["body"]["name"], ci_report.PRODUCTION)
        self.assertIn("Healthy in 1m 5s", check["body"]["output"]["text"])
        self.assertIn("@example-user", check["body"]["output"]["text"])
        self.assertEqual(comment["path"], f"repos/{REPO}/issues/12/comments")
        self.assertTrue(comment["body"]["body"].startswith(ci_report.DELIVERY_MARK.format(SHA)))

    def test_a_pull_request_already_told_is_not_told_again(self):
        _, writes, _ = self.report("success", comments=[ci_report.DELIVERY_MARK.format(SHA)])

        self.assertEqual([write["path"] for write in writes], [f"repos/{REPO}/check-runs"])

    def test_a_deploy_never_approved_is_not_blamed_on_a_step(self):
        verdict, writes, _ = self.report("failure")

        self.assertEqual((verdict.conclusion, verdict.title), ("neutral", "Not deployed: not approved"))
        self.assertEqual(len(writes), 1)

    def test_a_rejected_deploy_names_no_approver(self):
        rejected = {
            f"repos/{REPO}/actions/runs/42/approvals": [[{"state": "rejected", "user": {"login": "example-rejecter"}}]]
        }
        _, writes, _ = self.report("failure", answers=rejected)

        text = writes[0]["body"]["output"]["text"]
        approved = next(line for line in text.splitlines() if line.startswith("| **Approved** |"))
        self.assertNotIn("example-rejecter", approved)

    def test_cancelled_says_so(self):
        verdict, _, _ = self.report("cancelled")

        self.assertEqual(verdict.conclusion, "cancelled")

    def test_a_failed_deploy_is_diagnosed_once_its_log_is_served(self):
        verdict, writes, waits = self.report(
            "failure", reads=["", "health: starting\nNew image did not become healthy."], answers=APPROVED
        )

        self.assertEqual(verdict.conclusion, "failure")
        self.assertIn("previous image", verdict.summary)
        self.assertEqual(len(waits), 1)
        self.assertIn("[Compose](", writes[0]["body"]["output"]["text"])


class _Logs(BaseHTTPRequestHandler):
    seen: list

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("Authorization")))
        if self.path.endswith("/3/logs") and len(self.seen) == 1:
            # Rate limited once, then served.
            self.send_response(429)
            self.send_header("Retry-After", "7")
            self.end_headers()
        elif self.path.endswith(("/1/logs", "/3/logs")):
            self.send_response(302)
            self.send_header("Location", "/storage/1")
            self.end_headers()
        elif self.path == "/storage/1":
            body = b"the log"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.endswith("/4/logs"):
            self.send_response(503)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


@tag("source")
class JobLogTests(SimpleTestCase):
    def setUp(self):
        _Logs.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Logs)
        thread = threading.Thread(target=self.server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.api = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_the_token_goes_to_the_api_and_not_where_it_redirects(self):
        log = ci_report.job_log(1, token="secret-token", repo=REPO, api=self.api)

        self.assertEqual(log, "the log")
        self.assertEqual(
            _Logs.seen, [(f"/repos/{REPO}/actions/jobs/1/logs", "Bearer secret-token"), ("/storage/1", None)]
        )

    def test_a_log_not_served_is_reported_by_its_status_alone(self):
        with self.assertRaises(ci_report.LogUnreadable) as raised:
            ci_report.job_log(2, token="secret-token", repo=REPO, api=self.api)

        self.assertIn("404", str(raised.exception))
        self.assertEqual(len(_Logs.seen), 1)

    def test_a_rate_limit_is_waited_out_for_as_long_as_it_asks(self):
        waits = []

        log = ci_report.job_log(3, token="secret-token", repo=REPO, api=self.api, sleep=waits.append)

        self.assertEqual(log, "the log")
        self.assertEqual(waits, [7])

    def test_a_server_error_is_retried_after_a_growing_wait(self):
        waits = []

        with self.assertRaises(ci_report.LogUnreadable):
            ci_report.job_log(4, token="secret-token", repo=REPO, api=self.api, attempts=3, sleep=waits.append)

        self.assertEqual(len(_Logs.seen), 3)
        self.assertEqual(len(waits), 2)
        self.assertGreater(waits[0], 0)
        self.assertGreater(waits[1], waits[0])
