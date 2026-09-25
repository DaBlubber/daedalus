# Migrations

SQL files in this folder are applied by the collector (`run.py`) on every start,
in name order, each exactly once; the applied names are recorded in the table
`schema_migration`. A fresh database is created from `schema.sql`, which already
contains everything - write every migration so that running it again is harmless.

Name new files `YYYY-MM-DD_what.sql`. The folder is empty in the first public
release.
