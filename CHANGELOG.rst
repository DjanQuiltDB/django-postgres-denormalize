v1.0.0 (2026-10-18)
-------------------
Added:
 * ``postgres_denormalize.fields.DenormalizedForeignKey``, a foreign key that stores a copy of the foreign key at the
   end of a lookup path.
 * ``postgres_denormalize.fields.DenormalizedField``, a column that stores a copy of any other column at the end of a
   lookup path.
 * Backfill tools as a migration operation (``postgres_denormalize.operations.BackfillDenormalized``) and a function
   (``postgres_denormalize.operations.backfill_denormalized``).
