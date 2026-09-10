# Alembic scripts moved

Migration scripts live in the importable package ``schema_alembic/`` so
wheels/sdists can ship them without colliding with the third-party
``alembic`` library on ``sys.path``.

Configure via ``alembic.ini``:

```
script_location = schema_alembic
```
