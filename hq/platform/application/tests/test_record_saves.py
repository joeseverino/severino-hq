"""Every record saved with its links is held as its caller last read it."""

from django.test import TestCase

from ..assets import AssetCommand, save_asset
from ..security import cli_principal


class HeldAsReadTests(TestCase):
    def test_a_target_named_by_another_records_keyword_is_refused(self):
        with self.assertRaises(TypeError):
            save_asset(AssetCommand(item_name="Example box"), principal=cli_principal(), current_id=1)
