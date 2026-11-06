from django.db import migrations, models


def forget_surface_rules(apps, _schema_editor):
    """Drop every rule that covered a whole surface: a rule now names one agent."""

    apps.get_model("control_plane", "CapabilityRule").objects.filter(scope="surface").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0043_inventory_read_moment_only_answered"),
    ]

    operations = [
        # Going back restores no surface rule: which there were is gone with
        # them, and an earlier release reads the missing ones as its default.
        migrations.RunPython(forget_surface_rules, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="capabilityrule", name="unique_capability_rule"),
        migrations.AlterModelOptions(name="capabilityrule", options={"ordering": ("subject", "capability")}),
        # Every rule left is an agent's, which is what going back marks each one.
        migrations.AlterField(
            model_name="capabilityrule",
            name="scope",
            field=models.CharField(
                choices=[("surface", "Surface"), ("agent", "Agent")], default="agent", max_length=16
            ),
        ),
        migrations.RemoveField(model_name="capabilityrule", name="scope"),
        migrations.AddConstraint(
            model_name="capabilityrule",
            constraint=models.UniqueConstraint(fields=("subject", "capability"), name="unique_capability_rule"),
        ),
    ]
