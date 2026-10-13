from types import SimpleNamespace
from unittest import mock

import pgtrigger
from catalog.models import Category, Item, ItemAudit, Section
from django.apps import apps
from django.db import connection
from django.db.migrations.state import ProjectState
from django.test import SimpleTestCase, TestCase, override_settings
from tenancy.models import Project, Task, Tenant
from test_triggers import row_version

from postgres_denormalize.operations import BackfillDenormalized, backfill_denormalized


class BackfillTestCase(TestCase):
    """
    A row whose copy differs from its lookup path only exists when something wrote the row without the trigger, so the
    tests create such rows while the trigger is ignored.

    setUp creates the project Apollo with two tasks. The task Launch has the right project name, and the task Plan has
    the project name 'made up'.
    """

    def setUp(self):
        self.project = Project.objects.create(tenant=Tenant.objects.create(name='Acme'), name='Apollo')
        self.matching = Task.objects.create(project=self.project, title='Launch')
        self.differing = Task.objects.create(project=self.project, title='Plan')
        with pgtrigger.ignore('tenancy.Task:denormalize_set'):
            Task.objects.filter(pk=self.differing.pk).update(project_name='made up')

    def test_a_copy_that_differs_is_corrected_and_one_that_agrees_is_unchanged(self):
        """
        Case: backfill_denormalized runs for Task.
        Expected: The task Plan has the project name Apollo, and the task Launch is not written.
        """
        before = row_version(Task, self.matching.pk)

        backfill_denormalized(Task)

        self.assertEqual(Task.objects.get(pk=self.differing.pk).project_name, 'Apollo')
        self.assertEqual(row_version(Task, self.matching.pk), before)


class AlsoFillTestCase(TestCase):
    """
    setUp creates the section Hardware with the category Bolts, the item M6 in that category, and an audit row for the
    item whose section_title is empty.
    """

    def setUp(self):
        section = Section.objects.create(tenant=Tenant.objects.create(name='Acme'), title='Hardware')
        self.category = Category.objects.create(section=section, label='Bolts')
        item = Item.objects.create(category=self.category, name='M6', home_section=section)
        self.audit = ItemAudit.objects.create(item=item, category=self.category, section_title='')

    def test_an_audit_row_gets_the_current_value_of_its_own_lookup_path(self):
        """
        Case: backfill_denormalized runs for Item, with also_fill naming the section_title of ItemAudit.
        Expected: The audit row has the section title Hardware.
        """
        backfill_denormalized(Item, {ItemAudit: ['section_title']})

        self.assertEqual(ItemAudit.objects.get(pk=self.audit.pk).section_title, 'Hardware')

    def test_an_audit_row_whose_ancestor_is_gone_keeps_its_value(self):
        """
        Case: The audit row points at a category that does not exist and has the section title 'kept'.
              backfill_denormalized runs for Item, with also_fill naming the section_title of ItemAudit.
        Expected: The audit row still has the section title 'kept'.
        """
        ItemAudit.objects.filter(pk=self.audit.pk).update(category_id=999999, section_title='kept')

        backfill_denormalized(Item, {ItemAudit: ['section_title']})

        self.assertEqual(ItemAudit.objects.get(pk=self.audit.pk).section_title, 'kept')

    def test_a_field_that_is_not_denormalized_is_refused(self):
        """
        Case: backfill_denormalized runs for Item, with also_fill naming the plain foreign key item of ItemAudit.
        Expected: A TypeError that names catalog.ItemAudit.item.
        """
        with self.assertRaisesMessage(TypeError, 'catalog.ItemAudit.item is not a denormalized field'):
            backfill_denormalized(Item, {ItemAudit: ['item']})

    def test_the_operation_resolves_model_names_in_its_app_or_by_label(self):
        """
        Case: BackfillDenormalized runs in the app catalog with the model names item and itemaudit. Then the section
              title of the audit row is emptied, and the operation runs with the labels catalog.item and
              catalog.itemaudit.
        Expected: After each run, the audit row has the section title Hardware.
        """
        operation = BackfillDenormalized('item', also_fill={'itemaudit': ['section_title']})
        state = ProjectState.from_apps(apps)

        with connection.schema_editor() as schema_editor:
            operation.database_forwards('catalog', schema_editor, state, state)
        self.assertEqual(ItemAudit.objects.get(pk=self.audit.pk).section_title, 'Hardware')

        ItemAudit.objects.update(section_title='')
        with connection.schema_editor() as schema_editor:
            BackfillDenormalized('catalog.item', also_fill={'catalog.itemaudit': ['section_title']}).backfill(
                apps, schema_editor
            )
        self.assertEqual(ItemAudit.objects.get(pk=self.audit.pk).section_title, 'Hardware')


class RecordingRouter:
    """
    A database router that records the hints of every allow_migrate call and allows nothing, so no backfill runs.
    """

    def __init__(self):
        self.hints = []

    def allow_migrate(self, db, app_label, **hints):
        self.hints.append(hints)
        return False


class OperationTestCase(SimpleTestCase):
    def hints_for_router(self, operation):
        """
        Run ``operation`` forwards in the app catalog, and return the hints that the router receives.
        """
        router = RecordingRouter()
        schema_editor = SimpleNamespace(connection=SimpleNamespace(alias='default'))
        with override_settings(DATABASE_ROUTERS=[router]):
            operation.database_forwards('catalog', schema_editor, ProjectState(), ProjectState())
        return router.hints

    def test_the_router_receives_the_model_name(self):
        """
        Case: BackfillDenormalized('Item') runs forwards, with a router that records its hints.
        Expected: The router receives the lowercase model name item as the model_name hint, and nothing else.
        """
        self.assertEqual(self.hints_for_router(BackfillDenormalized('Item')), [{'model_name': 'item'}])

    def test_the_given_hints_add_to_the_model_name_and_can_replace_it(self):
        """
        Case: BackfillDenormalized('item') runs forwards with the hint database_group, and then with the hint model_name
              itemaudit.
        Expected: The router receives model_name item together with database_group, and then model_name itemaudit.
        """
        self.assertEqual(
            self.hints_for_router(BackfillDenormalized('item', hints={'database_group': 'reporting'})),
            [{'model_name': 'item', 'database_group': 'reporting'}],
        )
        self.assertEqual(
            self.hints_for_router(BackfillDenormalized('item', hints={'model_name': 'itemaudit'})),
            [{'model_name': 'itemaudit'}],
        )

    def test_a_model_given_with_its_app_label_gives_the_router_no_model_name(self):
        """
        Case: BackfillDenormalized('catalog.item') runs forwards, with a router that records its hints.
        Expected: The router receives no hints, because it pairs a model_name with the migration's own app label.
        """
        self.assertEqual(self.hints_for_router(BackfillDenormalized('catalog.item')), [{}])

    def test_the_operation_deconstructs_to_its_own_arguments(self):
        """
        Case: BackfillDenormalized is deconstructed with a model name, also_fill and hints, and then with only a model
              name.
        Expected: The keyword arguments are exactly the arguments that were passed. The model_name hint that the
                  operation adds for the router is not among them.
        """
        operation = BackfillDenormalized(
            'item', also_fill={'itemaudit': ['section_title']}, hints={'database_group': 'reporting'}
        )

        self.assertEqual(
            operation.deconstruct(),
            (
                'BackfillDenormalized',
                [],
                {
                    'model_name': 'item',
                    'also_fill': {'itemaudit': ['section_title']},
                    'hints': {'database_group': 'reporting'},
                },
            ),
        )
        self.assertEqual(
            BackfillDenormalized('item').deconstruct(), ('BackfillDenormalized', [], {'model_name': 'item'})
        )

    def test_the_operation_writes_to_the_database_of_the_migration(self):
        """
        Case: BackfillDenormalized('item') runs with a schema editor whose connection has the alias 'other', as it does
              for `migrate --database=other`.
        Expected: backfill_denormalized is called for Item with using='other'.
        """
        operation = BackfillDenormalized('item')
        operation.app_label = 'catalog'
        schema_editor = SimpleNamespace(connection=SimpleNamespace(alias='other'))

        with mock.patch('postgres_denormalize.operations.backfill_denormalized') as backfill:
            operation.backfill(apps, schema_editor)

        backfill.assert_called_once_with(Item, {}, using='other')

    def test_the_operation_describes_itself(self):
        """
        Case: BackfillDenormalized('item') describes itself.
        Expected: The description names the model item.
        """
        self.assertEqual(BackfillDenormalized('item').describe(), 'Backfill the denormalized fields of item')

    def test_reversing_runs_nothing(self):
        """
        Case: BackfillDenormalized('item') is checked for reversibility.
        Expected: The operation is reversible.
        """
        self.assertTrue(BackfillDenormalized('item').reversible)
