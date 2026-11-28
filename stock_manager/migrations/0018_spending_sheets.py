import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from ._adopt import ensure_models

# Order matters: the sheet and voucher tables must exist before the line table
# gains the columns that point at them.
NEW_MODELS = ("SpendingSheet", "IssueVoucher", "StockDocument", "StockDocumentLine")


def adopt_spending_schema(apps, schema_editor):
    ensure_models(schema_editor, NEW_MODELS)


def _user_fk():
    return models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)


class Migration(migrations.Migration):

    dependencies = [
        ('stock_manager', '0017_item_card_settings'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='SpendingSheet',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('number', models.CharField(max_length=40, unique=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('created_by', _user_fk()),
                    ],
                    options={
                        'verbose_name': 'Spending sheet',
                        'verbose_name_plural': 'Spending sheets',
                        'ordering': ('-created_at', '-id'),
                    },
                ),
                migrations.CreateModel(
                    name='IssueVoucher',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('number', models.CharField(max_length=80, unique=True)),
                        ('voucher_date', models.DateField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('oracle_posted_at', models.DateTimeField(blank=True, null=True)),
                        ('oracle_note', models.CharField(blank=True, default='', max_length=200)),
                        ('created_by', _user_fk()),
                        ('oracle_posted_by', _user_fk()),
                    ],
                    options={
                        'verbose_name': 'Issue voucher',
                        'verbose_name_plural': 'Issue vouchers',
                        'ordering': ('-created_at', '-id'),
                    },
                ),
                migrations.AddField(
                    model_name='stockdocument',
                    name='representative',
                    field=models.CharField(blank=True, default='', max_length=120),
                ),
                migrations.AddField(
                    model_name='stockdocumentline',
                    name='sheet',
                    field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='lines', to='stock_manager.spendingsheet'),
                ),
                migrations.AddField(
                    model_name='stockdocumentline',
                    name='voucher',
                    field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='lines', to='stock_manager.issuevoucher'),
                ),
                migrations.AlterField(
                    model_name='auditevent',
                    name='action',
                    field=models.CharField(choices=[('login', 'Signed in'), ('login_failed', 'Sign-in refused'), ('logout', 'Signed out'), ('password_changed', 'Password changed'), ('document_created', 'Document created'), ('document_approved', 'Document approved'), ('document_rejected', 'Document rejected'), ('document_permit', 'Company voucher recorded'), ('document_oracle_posted', 'Issue permit posted to Oracle'), ('sheet_created', 'Spending sheet created'), ('sheet_changed', 'Spending sheet changed'), ('voucher_recorded', 'Issue voucher number recorded'), ('voucher_cleared', 'Issue voucher number cleared'), ('voucher_oracle_posted', 'Issue voucher posted to Oracle'), ('item_created', 'Item created'), ('item_updated', 'Item changed'), ('item_deleted', 'Item deleted'), ('item_grouped', 'Oracle codes grouped under an internal code'), ('item_ungrouped', 'Oracle code moved to another internal code'), ('transfer_sent', 'Transfer requested'), ('transfer_completed', 'Transfer dispatched'), ('data_imported', 'Spreadsheet imported'), ('data_exported', 'Spreadsheet exported'), ('user_created', 'User created'), ('user_updated', 'User changed'), ('user_deactivated', 'User deactivated'), ('settings_changed', 'Settings changed'), ('release_published', 'App release published'), ('user_unlocked', 'Lockout cleared'), ('user_reset', 'Password reset on deploy'), ('phone_changed', 'Sign-in phone changed'), ('phone_requested', 'Phone change requested'), ('phone_request_decided', 'Phone change decided'), ('mobile_device_cleared', 'Phone unbound from the app')], max_length=32),
                ),
            ],
            database_operations=[
                migrations.RunPython(adopt_spending_schema, migrations.RunPython.noop)
            ],
        )
    ]
