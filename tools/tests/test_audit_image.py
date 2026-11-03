"""audit:image counts a container's files as overlayfs shows them: a later layer's copy, whiteout or
opaque folder hides what a layer below holds, and those bytes are counted as hidden, by part."""

import io
import tarfile

from django.test import SimpleTestCase

from tools import audit_image


def layer(*members: tuple[str, int], instruction: str = "RUN x") -> audit_image.Layer:
    """A layer archive holding each (path, size) as a file of that many bytes."""

    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w") as archive:
        for path, size in members:
            info = tarfile.TarInfo(path)
            info.size = size
            archive.addfile(info, io.BytesIO(b"x" * size))
    blob.seek(0)
    return audit_image.read_layer(blob, instruction)


class ShownTests(SimpleTestCase):
    def test_a_collected_tree_cleared_and_collected_again_is_seen_once_and_its_first_copy_hidden(self) -> None:
        layers = [
            layer(("static/css/app.css", 300), ("static/js/old.js", 200), ("usr/lib/libc.so", 50)),
            layer(("static/.wh..wh..opq", 0), ("static/css/app.css", 310), ("static/js/new.js", 100)),
        ]

        files, hidden = audit_image.shown(layers)

        self.assertEqual(files, {"static/css/app.css": 310, "static/js/new.js": 100, "usr/lib/libc.so": 50})
        self.assertEqual(hidden, [{"/static": 500}, {}])

    def test_a_whiteout_hides_the_file_or_folder_it_names_and_nothing_beside_it(self) -> None:
        layers = [
            layer(("app/hq/a.py", 10), ("app/hq/b/c.py", 20), ("app/hq/keep.py", 5)),
            layer(("app/hq/.wh.a.py", 0), ("app/hq/.wh.b", 0)),
        ]

        files, hidden = audit_image.shown(layers)

        self.assertEqual(files, {"app/hq/keep.py": 5})
        self.assertEqual(hidden[0], {"/app/hq": 30})

    def test_an_opaque_folder_keeps_its_own_layer_s_files(self) -> None:
        files, _ = audit_image.shown([layer(("static/.wh..wh..opq", 0), ("static/a.css", 7))])

        self.assertEqual(files, {"static/a.css": 7})


class PartTests(SimpleTestCase):
    def test_each_file_counts_toward_the_part_it_belongs_to(self) -> None:
        for path, part in (
            ("app/.venv/lib/python3.14/site-packages/django/db/models.py", "site-packages"),
            ("usr/local/lib/python3.14/site-packages/pip/x.py", "site-packages"),
            ("usr/local/lib/python3.14/json/decoder.py", "stdlib"),
            ("usr/local/bin/hq-controller", "hq-controller"),
            ("app/static/css/app.css", "/app/static"),
            ("static/css/app.css", "/static"),
            ("app/hq/platform/core/static.py", "/app/hq"),
            ("usr/lib/aarch64-linux-gnu/libssl.so.3", "/usr/lib/aarch64-linux-gnu"),
        ):
            with self.subTest(path=path):
                self.assertEqual(audit_image.bucket(path), part)

    def test_a_distribution_s_metadata_counts_with_its_package(self) -> None:
        site = "app/.venv/lib/python3.14/site-packages/"
        self.assertEqual(audit_image.package(site + "Django-5.2.dist-info/RECORD"), "django")
        self.assertEqual(audit_image.package(site + "django/__init__.py"), "django")
        self.assertIsNone(audit_image.package("usr/lib/x.so"))


class ReferenceTests(SimpleTestCase):
    def test_a_word_docker_could_read_as_a_flag_is_refused_before_docker_runs(self) -> None:
        for word in ("--output=/etc/x", "-o", "a b", ""):
            with self.subTest(word=word):
                self.assertEqual(audit_image.main(word), 2)
