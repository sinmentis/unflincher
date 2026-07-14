# Importing existing entries

If you are migrating from a Tofu Chrome extension export of your Douban diary, pass the untouched
`.xlsx` export to the importer. The importer recognizes the extension's original diary sheet and
column layout, so you do not need to rename anything in the workbook.

## Production import

Run the importer once, before the service is first started, so there are never two writers to the
SQLite database:

```bash
deploy/scripts/import-unflincher.sh /path/to/your-export.xlsx
```

The wrapper stages the workbook in a repository-local `import/` directory, mounts that directory
read-only at `/import` inside the container, writes only the resulting SQLite database to the
`unflincher-data` volume, and prints the entry count.

## Local import

For a local development database, call the CLI directly against the file it should write:

```bash
.venv/bin/python -m unflincher.cli import --excel /path/to/your-export.xlsx --db unflincher.dev.db
```

## No existing archive

If you are not migrating from Douban, skip this entirely. New entries can always be typed directly
into the New Entry page in the app.
