"""Each scan of tools/source_scans.py finds its form in every way a tool writes it, and passes the right one."""

from django.test import SimpleTestCase, tag

from tools.source_scans import mise_trust_findings, python_findings, shell_findings


@tag("source")
class SourceScanCaseTests(SimpleTestCase):
    """Each scan finds its form in every way a tool writes it, and passes the right one."""

    def test_a_shell_git_list_without_z_is_found_and_one_with_it_passes(self):
        text = (
            "git ls-files --others | while read -r f; do :; done\n"
            "git -C dir diff --name-only \\\n    main\n"
            "git ls-files -z | tr '\\0' '\\n'\n"
            '[ -z "$(git log --format=%H)" ]\n'
        )
        self.assertEqual([line for line, _ in shell_findings(text)], [1, 2])

    def test_a_python_git_call_list_or_string_without_z_is_found_wherever_it_wraps(self):
        text = (
            'git(\n    "diff",\n    "--name-only",\n    base,\n)\n'
            'run(["git", "ls-files"])\n'
            'run(["git", "ls-files", "-z"])\n'
            'SCRIPT = "x=$(git log --name-only)"\n'
            'def f():\n    """git ls-files, in prose."""\n'
        )
        self.assertEqual(sorted(line for line, _ in python_findings(text)), [1, 6, 8])

    def test_each_way_of_setting_the_mise_trust_is_found(self):
        text = "\n".join(
            (
                'MISE_TRUSTED_CONFIG_PATHS="${bound}" mise -C "${bound}" run test',
                "export MISE_TRUSTED_CONFIG_PATHS=/tmp/project",
                'env = {**os.environ, "MISE_TRUSTED_CONFIG_PATHS": config}',
                "env = clean_env(MISE_TRUSTED_CONFIG_PATHS=str(tree))",
                "os.environ['MISE_TRUSTED_CONFIG_PATHS'] = config",
                "ENV MISE_TRUSTED_CONFIG_PATHS=/tools",
            )
        )
        self.assertEqual([line for line, _ in mise_trust_findings("a.sh", text)], [1, 2, 3, 4, 5, 6])

    def test_a_read_of_the_mise_trust_passes(self):
        text = 'trusted = os.environ.get("MISE_TRUSTED_CONFIG_PATHS")\necho "${MISE_TRUSTED_CONFIG_PATHS}"\n'
        self.assertEqual(mise_trust_findings("a.sh", text), [])
