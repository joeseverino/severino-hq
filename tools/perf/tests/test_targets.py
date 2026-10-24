"""perf:report reads a task's stdout only where mise prints it unlabeled, and
tells a server that refuses it from one still starting."""

import ast
import inspect
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override

from django.test import SimpleTestCase, tag

from tests.mise_tasks import mise_tasks
from tools.perf import targets


def calls(method: str) -> list[tuple[str, ast.Call]]:
    """Each ``self.<method>(...)`` in the targets module, with the function it is in."""

    functions = [node for node in ast.walk(ast.parse(inspect.getsource(targets))) if isinstance(node, ast.FunctionDef)]
    return [
        (function.name, node)
        for function in functions
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]


@tag("source")
class TaskReadTests(SimpleTestCase):
    def test_a_task_is_read_only_through_read(self):
        """A server's own output goes to its log; every other mise run is read, so goes through _read."""

        callers = {function for function, _ in calls("_mise")}
        self.assertEqual(callers, {"_read", "_start"})

    def test_every_task_read_prints_its_own_stdout(self):
        """Under any other output style mise labels each line, and the label is read as the output."""

        read = [call.args[0] for _, call in calls("_read")]
        self.assertTrue(read)
        tasks = mise_tasks()
        for argument in read:
            with self.subTest(line=argument.lineno):
                self.assertIsInstance(argument, ast.Constant, "name the task read as a literal")
                self.assertTrue(tasks[argument.value]["raw"], f"{argument.value} is read, so it is raw")


def answering(status: int) -> ThreadingHTTPServer:
    """A loopback server answering every GET with ``status``, serving until shut down."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()

        @override
        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ReadinessTests(SimpleTestCase):
    def wait(self, status: int) -> float:
        server = answering(status)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return targets.wait_ready(f"http://{host}:{port}", targets.FIRST_BOOT_S)

    def test_ready_is_a_200(self):
        self.assertGreaterEqual(self.wait(200), 0)

    def test_a_refusal_fails_at_once_rather_than_at_the_deadline(self):
        for status in (400, 403):
            with self.subTest(status=status), self.assertRaisesRegex(targets.NotMeasured, str(status)):
                self.wait(status)
