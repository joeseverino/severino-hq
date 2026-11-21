import django.db.models.expressions
import django.db.models.functions.math
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0006_drop_help_text"),
    ]

    # A generated column is never altered in place: it is dropped and added again.
    operations = [
        migrations.RemoveField(
            model_name="asset",
            name="estimated_deductible_amount",
        ),
        migrations.AlterField(
            model_name="asset",
            name="total_cost",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="asset",
            name="estimated_deductible_amount",
            field=models.GeneratedField(
                db_persist=True,
                expression=django.db.models.expressions.CombinedExpression(
                    django.db.models.functions.math.Round(
                        django.db.models.expressions.CombinedExpression(
                            django.db.models.expressions.CombinedExpression(
                                django.db.models.functions.math.Round(
                                    django.db.models.expressions.CombinedExpression(
                                        models.F("total_cost"), "*", models.Value(100)
                                    ),
                                    output_field=models.FloatField(),
                                ),
                                "*",
                                models.F("business_use_percentage"),
                            ),
                            "/",
                            models.Value(100.0),
                        )
                    ),
                    "/",
                    models.Value(100.0),
                ),
                null=True,
                output_field=models.DecimalField(decimal_places=2, max_digits=12),
            ),
        ),
    ]
