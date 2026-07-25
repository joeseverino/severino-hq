"""Idempotently create or update content through the application service."""

from __future__ import annotations

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from application.content import ContentCommand, save_content
from application.security import cli_principal
from content.models import ContentItem


class Command(BaseCommand):
    help = "Create or update a ContentItem by slug."

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("--title", required=True)
        parser.add_argument(
            "--type",
            dest="content_type",
            choices=[choice.value for choice in ContentItem.Type],
            default=ContentItem.Type.ARTICLE,
        )
        parser.add_argument(
            "--status",
            choices=[choice.value for choice in ContentItem.Status],
            default=ContentItem.Status.DRAFT,
        )
        parser.add_argument("--topic", default="")
        parser.add_argument("--tags", default="")
        parser.add_argument("--url", dest="published_url", default="")
        parser.add_argument("--published-at")
        parser.add_argument("--notes", default="")
        parser.add_argument("--project", action="append", default=[])
        parser.add_argument("--asset", action="append", default=[])
        parser.add_argument("--expense", type=int, action="append", default=[])
        parser.add_argument("--doc", action="append", default=[])
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        try:
            published_at = (
                date.fromisoformat(options["published_at"])
                if options["published_at"]
                else None
            )
        except ValueError as exc:
            raise CommandError("--published-at must use YYYY-MM-DD") from exc

        slug = options["slug"]
        exists = ContentItem.objects.filter(slug=slug).exists()
        result = save_content(
            ContentCommand(
                title=options["title"],
                slug=slug,
                content_type=options["content_type"],
                status=options["status"],
                topic=options["topic"],
                tags=options["tags"],
                published_url=options["published_url"],
                published_at=published_at,
                notes=options["notes"],
                related_projects=tuple(options["project"]),
                related_assets=tuple(options["asset"]),
                related_expenses=tuple(options["expense"]),
                related_documentation=tuple(options["doc"]),
            ),
            principal=cli_principal(),
            current_slug=slug if exists else None,
        )
        if options["json"]:
            self.stdout.write(json.dumps(result))
            return
        verb = "created" if result["created"] else "updated"
        self.stdout.write(f"Content {result['content']['slug']}: {verb}")
