"""
Columns that store a copy of a value at the end of a chain of foreign keys, kept equal to it by PostgreSQL triggers.

Terms that are used throughout this package:

- A *denormalized field* is a ``DenormalizedForeignKey``, which copies a foreign key, or a ``DenormalizedField``, which
  copies any other column. The model that declares it is the *leaf*.
- The field's ``denormalize_from`` is a *lookup path* such as ``'project__tenant'`` or ``'project__name'``: a chain of
  forward foreign keys that starts on the leaf. It ends at the column whose value the field copies, which is the
  *source column*.
- Each foreign key along the lookup path is a *hop*. The first hop is a column of the leaf itself. The model that has a
  later hop, or the source column, is the *owner* of that segment.

A model declares the field and nothing else::

    class Task(models.Model):
        project = models.ForeignKey(Project, on_delete=models.CASCADE)
        tenant = DenormalizedForeignKey('project__tenant', on_delete=models.CASCADE)
        project_name = DenormalizedField('project__name')

The package has these modules:

- ``chains`` walks a lookup path.
- ``fields`` has both kinds of denormalized field, and the helpers that find a model's denormalized fields and compare
  their stored values with their lookup paths.
- ``triggers`` has the triggers that keep every denormalized field correct, and the registration that derives them from
  the fields while ``PostgresDenormalizeConfig.ready()`` runs.
- ``operations`` has ``BackfillDenormalized``, the migration operation that corrects stored values before a migration
  installs or changes the triggers.
- ``batch`` has ``ignore_cascades`` and ``defer_cascades``, which skip the cascade triggers during a batch update and
  correct the copies in one set-based pass when the batch ends. Inside the block, those copies are out of date.
- ``checks`` has the system checks. ``conf`` has the ``POSTGRES_DENORMALIZE`` setting, including ``EXCLUDE_MODELS``
  and ``EXCLUDE_MODELS_CALLBACK``, which name the models that this package skips.
"""

__version__ = '1.0.1'
