"""One-off lockout reset requested by the operator.

The deployed host has no shell, and the account was locked out after the
attempt limit. Clearing the axes rows is the same action as the «فتح القفل»
button on /users/, but it has to travel with a deploy when nobody can reach
that page. The migration is recorded once it runs, so it cannot weaken a later
lockout — it only removes the rows that existed when it was applied.
"""

from django.db import migrations

USERNAME = "lifeendy"


def unlock(apps, schema_editor):
    try:
        AccessAttempt = apps.get_model("axes", "AccessAttempt")
    except LookupError:
        return
    AccessAttempt.objects.filter(username__iexact=USERNAME).delete()


class Migration(migrations.Migration):
    dependencies = [("stock_manager", "0013_item_card_fields")]

    operations = [migrations.RunPython(unlock, migrations.RunPython.noop)]
