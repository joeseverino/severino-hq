"""perf:report reads a task's stdout only where mise prints it unlabeled."""

import ast
import inspect

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
