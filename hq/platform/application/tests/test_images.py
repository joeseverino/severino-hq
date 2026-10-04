from __future__ import annotations

from django.test import SimpleTestCase

from ..images import ImageRef, affected, newer, shape, version


class ReferenceTests(SimpleTestCase):
    def test_a_reference_names_its_registry_repository_tag_and_digest(self):
        found = ImageRef.parse("ghcr.io/example/app:v2.16.0@sha256:abc")

        self.assertEqual(
            (found.registry, found.repository, found.tag, found.digest, found.github),
            ("ghcr.io", "example/app", "v2.16.0", "sha256:abc", "example/app"),
        )

    def test_docker_hub_is_the_default_and_official_images_live_under_library(self):
        found = ImageRef.parse("nginx:1.31.3-alpine")

        self.assertEqual((found.name, found.short, found.github), ("docker.io/library/nginx", "nginx", ""))

    def test_a_registry_is_told_apart_from_a_namespace_by_looking_like_a_host(self):
        self.assertEqual(ImageRef.parse("localhost:5000/team/app:1").registry, "localhost:5000")
        self.assertEqual(ImageRef.parse("team/app").registry, "docker.io")
        self.assertEqual(ImageRef.parse("team/app@sha256:abc").tag, "")

    def test_an_image_id_is_no_reference(self):
        self.assertIsNone(ImageRef.parse("sha256:abc"))
        self.assertIsNone(ImageRef.parse(""))


class VersionTests(SimpleTestCase):
    def test_only_tags_of_the_same_shape_compare(self):
        tags = ["1.31.4-alpine", "1.33.0", "1.32.0-alpine", "mainline-alpine", "1.31.3-alpine", "1.32.0-alpine-slim"]

        self.assertEqual(newer("1.31.3-alpine", tags), ["1.32.0-alpine", "1.31.4-alpine"])
        self.assertEqual(shape("v2.16.0"), "vN.N.N")

    def test_a_major_only_tag_compares_majors_and_a_word_compares_nothing(self):
        self.assertEqual(newer("1", ["1", "2", "2.0.0"]), ["2"])
        self.assertEqual(newer("latest", ["latest", "2"]), [])
        self.assertEqual(version("latest"), ())


class AdvisoryTests(SimpleTestCase):
    def test_a_version_inside_a_range_is_affected(self):
        self.assertTrue(affected("v2.16.0", [(">= 2.0.0, < 2.17.1", "2.17.1")]))
        self.assertFalse(affected("v2.18.0", [("< 2.17.1", "2.17.1")]))

    def test_a_stated_fix_clears_an_open_ended_range(self):
        # As published: the range left open, the fix stated beside it.
        self.assertFalse(affected("v2.16.0", [(">= v2.2.0", "v2.12.0")]))

    def test_a_fix_on_another_major_line_does_not_clear_this_one(self):
        self.assertTrue(affected("2.0.0", [("< 2.1.3", "1.8.5, 2.1.3")]))
        self.assertFalse(affected("1.9.0", [("< 2.1.3", "1.8.5, 2.1.3")]))

    def test_ranges_as_publishers_write_them(self):
        self.assertTrue(affected("7.10.0", [(">= 7.5.0 && < 7.15.2", "")]))
        self.assertFalse(affected("v2.5.0", [("v2.0.0 - v.2.4.0", "")]))
        self.assertTrue(affected("7.0.1", [("7.0.0, 7.0.1", "")]))
        self.assertTrue(affected("5.0", [("3.0.0 < 6.1.1", "")]))

    def test_what_cannot_be_read_is_not_known_rather_than_safe(self):
        self.assertIsNone(affected("7.15.4", [("All", "")]))
        self.assertIsNone(affected("latest", [("< 1.0", "")]))
        self.assertFalse(affected("7.15.4", [("All", ""), ("< 5.1.0", "5.1.0")]))
