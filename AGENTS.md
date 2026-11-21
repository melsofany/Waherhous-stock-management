# Waherhous stock management — repository notes

Orascom Construction (أوراسكوم) warehouse app. Django 6.1.3 + DRF, SAP-Fiori-style
UI (Arabic RTL / English LTR), deployed on Render as `ssm-warehouse`
(`https://ssm-warehouse.onrender.com`). Android app consumes the same API.

## Running it locally

`ssm/settings.py` requires several env vars — a bare `manage.py` fails:

```bash
export DJANGO_DEBUG=True ALLOW_PW_CHANGE=False DJANGO_SECRET_KEY=dev-secret \
       DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1 \
       AXES_FAILURE_LIMIT=50 AXES_COOLOFF_TIME=1 LOG_FILE=/tmp/dev.log \
       DB_NAME=dev.sqlite3
python manage.py migrate --run-syncdb --fake-initial --noinput
python manage.py runserver 127.0.0.1:8911 --noreload
```

- `DJANGO_DEBUG=False` turns on `SECURE_SSL_REDIRECT`, so a browser against the
  plain-HTTP dev port fails with `ERR_SSL_PROTOCOL_ERROR`. Use `True` locally.
- Templates/static changes are **not** picked up by a `--noreload` server that was
  started before the edit — restart it before measuring or screenshotting
  (this bit twice: a CSS/template edit "did nothing" until the restart).

## Migrations and the live database

- `migrations/` used to be gitignored, so the live database has **no
  `stock_manager` rows in `django_migrations`**; its tables were created by
  `--run-syncdb`. A new column can only arrive through a migration, so the
  directory is now committed.
- The Render build therefore runs
  `python manage.py migrate --run-syncdb --fake-initial --noinput`:
  `--fake-initial` records `0001_initial` as applied instead of trying to CREATE
  tables that already exist, and later migrations still run.
  **Verified** against a database built from the previous release: `0001 FAKED`,
  `0002 OK`, existing rows kept with an empty unit.
- Never put a `#` comment inside the folded `buildCommand: >-` YAML block — it
  becomes literal shell text and comments out the rest of the command. Put notes
  above the key.
- `--run-syncdb` creates **new tables** only; it never alters an existing one.

## Unit of measure (وحدة القياس)

`Item.unit` + `StockDocumentLine.unit` (+ `GoodsReceiptLine.unit`), free text with
suggestions (`COMMON_UNITS` in `documents.py`). Rules that the tests pin:

- a line that states no unit inherits the item's,
- an addition for an unknown SKU **teaches** the new item its unit,
- a booked line is a snapshot: changing the catalogue later never rewrites it,
- the unit is shown on stock/site tables, the issues list, the report screen and
  the Excel `Unit` column.

## Tests

`python -m pytest -q tests/` — 392 pass, 3 skip (a user-supplied, gitignored
`spreadsheet_convert.py`). `pip install Pillow` and
`python -m playwright install chromium` are needed for the full run; the e2e
suites skip themselves when Chromium is missing.

## Git / push

`$GITHUB_TOKEN` in this sandbox is a `ghu_` OAuth token with read-only access:
`git push` returns `403 Permission … denied` even though `/repos/...` reports
`permissions.push: true`. `$OH_GIT_CREDENTIAL_ACCESS_TOKEN_GITHUB` is invalid
(401). A fresh user-issued `ghp_` token with `repo` scope is required to push and
open a PR.
