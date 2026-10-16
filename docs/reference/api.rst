=============
API reference
=============

The guides explain what each piece is for. This page is the reference for what it takes and returns.


Fields
======

The fields that a leaf declares, and the functions that read them.

.. currentmodule:: postgres_denormalize.fields

.. autoclass:: DenormalizedForeignKey

.. autoclass:: DenormalizedField

.. autoclass:: DenormalizedMixin

.. autofunction:: get_denormalized_fields

.. autofunction:: drift_condition

.. autoclass:: IsDistinctFrom


Migration operations
====================

The backfill that fills a new copy and corrects copies that differ from their paths. See :doc:`/guides/migrations`.

.. currentmodule:: postgres_denormalize.operations

.. autoclass:: BackfillDenormalized

.. autofunction:: backfill_denormalized


Triggers
========

The pgtrigger triggers that the fields derive. A project does not declare them itself. See :doc:`/guides/triggers`.

.. currentmodule:: postgres_denormalize.triggers

.. autoclass:: DenormalizeSetTrigger

.. autoclass:: DenormalizeCascadeTrigger

.. autoclass:: DenormalizeFillTrigger

.. autofunction:: cascade_trigger_name
