"""Create or update a Project record from the CLI.

Idempotent — re-running with the same slug updates the existing record.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from application.projects import ProjectCommand, save_project
from projects.models import PROJECT_CATEGORY_CHOICES, Project


class Command(BaseCommand):
    help = "Create or update a Project record by slug."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="URL slug (e.g. jseverino-public-site).")
        parser.add_argument(
            "--name",
            required=True,
            help="Display name (free text).",
        )
        parser.add_argument(
            "--category",
            choices=[c[0] for c in PROJECT_CATEGORY_CHOICES],
            default="other",
        )
        parser.add_argument(
            "--status",
            choices=[c.value for c in Project.Status],
            default=Project.Status.IDEA,
        )
        parser.add_argument("--description", default="")
        parser.add_argument(
            "--technologies",
            default="",
            help="Comma-separated list of technologies.",
        )
        parser.add_argument("--repo", dest="repository_url", default="")
        parser.add_argument("--url", dest="public_url", default="")
        parser.add_argument("--notes", default="")
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print the canonical service result as JSON.",
        )

    def handle(self, *args, **opts):
        slug = opts["slug"]
        exists = Project.objects.filter(slug=slug).exists()
        result = save_project(
            ProjectCommand(
                name=opts["name"],
                slug=slug,
                category=opts["category"],
                status=opts["status"],
                description=opts["description"],
                technologies_used=opts["technologies"],
                repository_url=opts["repository_url"],
                public_url=opts["public_url"],
                notes=opts["notes"],
            ),
            interface="cli",
            actor="local-operator",
            current_slug=slug if exists else None,
        )
        if opts["json"]:
            self.stdout.write(json.dumps(result))
            return
        verb = "created" if result["created"] else "updated"
        self.stdout.write(f"Project {result['project']['slug']}: {verb}")
