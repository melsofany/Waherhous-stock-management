"""Item card direction and paper orientation.

The project name/code already lived on the configuration row; these two
settings belong beside them because every card is printed with the same
direction and paper. The database work adopts the schema, so a live database
that lacks the columns still deploys.
"""

from django.db import migrations, models

from ._adopt import ensure_models


class Migration(migrations.Migration):

    dependencies = [
        ("stock_manager", "0016_issue_permit_workflow"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="admin",
                    name="card_direction",
                    field=models.CharField(
                        choices=[
                            ("rtl", "Right to left"),
                            ("ltr", "Left to right"),
                        ],
                        db_default="rtl",
                        default="rtl",
                        max_length=3,
                    ),
                ),
                migrations.AddField(
                    model_name="admin",
                    name="card_orientation",
                    field=models.CharField(
                        choices=[
                            ("portrait", "Portrait"),
                            ("landscape", "Landscape"),
                        ],
                        db_default="portrait",
                        default="portrait",
                        max_length=9,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunPython(
                    lambda apps, schema_editor: ensure_models(
                        schema_editor, ("Admin",)
                    ),
                    migrations.RunPython.noop,
                )
            ],
        ),
    ]
