v1.1.0 (2026-11-10)
-------------------
Added:
 * Support for Python 3.12 and 3.13.
 * Support for Python 3.15 (Django 6.1.3 or later).

v1.0.2 (2026-11-09)
-------------------
Added:
 * Coverage badge in the README.

Altered:
 * Fixed the documentation badge in the README rendering as a broken image on GitHub.
 * Fixed a minor packaging bug where the sdist would ship coverage data if present.

v1.0.1 (2026-10-19)
-------------------
Altered:
 * Fixed system checks incorrectly firing on excluded models.

v1.0.0 (2026-10-18)
-------------------
Added:
 * ``postgres_denormalize.fields.DenormalizedForeignKey``, a foreign key that stores a copy of the foreign key at the
   end of a lookup path.
 * ``postgres_denormalize.fields.DenormalizedField``, a column that stores a copy of any other column at the end of a
   lookup path.
 * Backfill tools as a migration operation (``postgres_denormalize.operations.BackfillDenormalized``) and a function
   (``postgres_denormalize.operations.backfill_denormalized``).
