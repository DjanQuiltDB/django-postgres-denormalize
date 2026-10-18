from django.db import models
from django.test import SimpleTestCase
from django.test.utils import isolate_apps

from postgres_denormalize.fields import DenormalizedField
from postgres_denormalize.operations import lookup_path_value


@isolate_apps('isolated')
class ToFieldTestCase(SimpleTestCase):
    def test_the_backfill_reads_the_ancestor_by_code(self):
        """
        Case: Leaf's foreign key to Parent stores the parent's unique code, and Leaf copies parent__name. The value that
              the backfill reads for the copy is compiled into SQL.
        Expected: The subquery finds the parent by its code, not by its primary key.
        """

        class Parent(models.Model):
            code = models.CharField(max_length=10, unique=True)
            name = models.CharField(max_length=40)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE, to_field='code')
            parent_name = DenormalizedField('parent__name')

        value = lookup_path_value(Leaf, Leaf._meta.get_field('parent_name'))
        sql = str(Leaf._base_manager.annotate(value=value).query)

        # Django 6.1 quotes the alias of the subquery's table, and Django 6.0 does not, so only the column is compared.
        self.assertIn('."code" = ("isolated_leaf"."parent_id")', sql)
