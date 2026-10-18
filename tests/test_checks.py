import copy
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.core import checks
from django.db.backends.postgresql.psycopg_any import IsolationLevel
from django.db.utils import ConnectionHandler
from django.test import SimpleTestCase, override_settings
from tenancy.models import Task

from postgres_denormalize.checks import (
    _checked_models,
    check_excluded_models_exist,
    check_isolation_level,
    check_ordering_uses_copies,
    check_owners_outside_the_project,
    check_redundant_copies,
    check_triggers_reach_the_database,
    crosses_lookup_path,
)


class TriggersReachTheDatabaseTestCase(SimpleTestCase):
    def test_migrations_are_enough(self):
        """
        Case: PGTRIGGER_MIGRATIONS has its default value, which is on.
        Expected: No error.
        """
        self.assertEqual(check_triggers_reach_the_database(), [])

    @override_settings(PGTRIGGER_MIGRATIONS=False, PGTRIGGER_INSTALL_ON_MIGRATE=True)
    def test_installing_after_migrate_is_enough(self):
        """
        Case: PGTRIGGER_MIGRATIONS is off, and PGTRIGGER_INSTALL_ON_MIGRATE is on.
        Expected: No error.
        """
        self.assertEqual(check_triggers_reach_the_database(), [])

    @override_settings(PGTRIGGER_MIGRATIONS=False)
    def test_neither_is_an_error(self):
        """
        Case: PGTRIGGER_MIGRATIONS and PGTRIGGER_INSTALL_ON_MIGRATE are both off.
        Expected: Error E301.
        """
        self.assertEqual([error.id for error in check_triggers_reach_the_database()], ['postgres_denormalize.E301'])


class InstalledProjectTestCase(SimpleTestCase):
    def test_the_test_project_passes_every_check(self):
        """
        Case: The checks with the postgres_denormalize tag run on the test project.
        Expected: No messages.
        """
        self.assertEqual(checks.run_checks(tags=['postgres_denormalize']), [])

    def test_third_party_apps_are_not_checked_by_default(self):
        """
        Case: The project-wide checks collect their models without app_configs and without APPS.
        Expected: The models of tenancy are checked, and the models of auth and contenttypes, which are installed in
                  site-packages, are not.
        """
        labels = {model._meta.app_label for model in _checked_models(None, None)}

        self.assertIn('tenancy', labels)
        self.assertNotIn('auth', labels)
        self.assertNotIn('contenttypes', labels)

    def test_apps_can_be_named(self):
        """
        Case: The project-wide checks collect their models with APPS set to ['tenancy'].
        Expected: Only the models of tenancy are checked.
        """
        labels = {model._meta.app_label for model in _checked_models(None, ['tenancy'])}

        self.assertEqual(labels, {'tenancy'})

    def test_excluded_models_are_skipped(self):
        """
        Case: The project-wide checks collect the models of catalog, where EXCLUDE_MODELS lists ItemAudit, and where
              Reviewer is a proxy model.
        Expected: Item, Category, Section and Review are checked, and ItemAudit and Reviewer are not.
        """
        names = {model.__name__ for model in _checked_models([apps.get_app_config('catalog')], None)}

        self.assertEqual(names, {'Item', 'Category', 'Section', 'Review'})

    @override_settings(
        POSTGRES_DENORMALIZE={**settings.POSTGRES_DENORMALIZE, 'REDUNDANT_COPY_CHECK': {'APPS': ['tenancy', 'typo']}}
    )
    def test_an_apps_entry_that_names_no_installed_app_is_an_error(self):
        """
        Case: The redundant copy check runs with APPS set to ['tenancy', 'typo'], and no installed app has the label
              typo.
        Expected: One error E402, which names typo, and no exception.
        """
        messages = check_redundant_copies()

        self.assertEqual([message.id for message in messages], ['postgres_denormalize.E402'])
        self.assertIn('typo', messages[0].msg)

    @override_settings(
        POSTGRES_DENORMALIZE={
            'EXCLUDE_MODELS_CALLBACK': 'catalog.exclusions.is_audit_model',
            'REDUNDANT_COPY_CHECK': {'ALLOWED': {}},
        }
    )
    def test_without_the_allowlist_the_test_project_reports_its_deliberate_route(self):
        """
        Case: The redundant copy check runs on the test project with an empty ALLOWED.
        Expected: One warning W101, for Item.home_section, the foreign key that the test settings list in ALLOWED.
                  Item.category is not reported: the only other route to a category, parent_category__parent, crosses
                  the copy parent_category, which no lookup path may cross.
        """
        messages = check_redundant_copies()

        self.assertEqual([message.obj.name for message in messages], ['home_section'], [m.msg for m in messages])
        self.assertEqual({message.id for message in messages}, {'postgres_denormalize.W101'})


class ExcludedModelsExistTestCase(SimpleTestCase):
    def test_the_test_project_lists_only_installed_models(self):
        """
        Case: The check runs on the test project, whose EXCLUDE_MODELS lists catalog.ItemAudit.
        Expected: No error.
        """
        self.assertEqual(check_excluded_models_exist(), [])

    @override_settings(POSTGRES_DENORMALIZE={'EXCLUDE_MODELS': ['catalog.ItemAudit', 'catalog.Missing']})
    def test_an_entry_that_names_no_installed_model_is_an_error(self):
        """
        Case: EXCLUDE_MODELS lists catalog.ItemAudit and catalog.Missing, and no installed model has the label
              catalog.Missing.
        Expected: One error E401, which names catalog.Missing.
        """
        messages = check_excluded_models_exist()

        self.assertEqual([message.id for message in messages], ['postgres_denormalize.E401'])
        self.assertIn('catalog.Missing', messages[0].msg)


class CrossesLookupPathTestCase(SimpleTestCase):
    def test_the_path_its_id_column_and_longer_paths_cross_it(self):
        """
        Case: The orderings project__tenant, project__tenant_id and project__tenant__name of Task are compared with the
              lookup path project__tenant.
        Expected: Each of them crosses the lookup path.
        """
        for path in ('project__tenant', 'project__tenant_id', 'project__tenant__name'):
            with self.subTest(path=path):
                self.assertTrue(crosses_lookup_path(Task, path, 'project__tenant'))

    def test_a_shorter_or_diverging_path_does_not_cross_it(self):
        """
        Case: The orderings project, project__tenantship and project__name of Task are compared with the lookup path
              project__tenant.
        Expected: None of them crosses the lookup path.
        """
        for path in ('project', 'project__tenantship', 'project__name'):
            with self.subTest(path=path):
                self.assertFalse(crosses_lookup_path(Task, path, 'project__tenant'))


class OrderingCheckTestCase(SimpleTestCase):
    def test_the_test_project_orders_through_its_copies(self):
        """
        Case: The ordering check runs on the test project, where Task orders by its copy project_name.
        Expected: No message.
        """
        self.assertEqual(check_ordering_uses_copies(), [])


class OwnersOutsideTheProjectTestCase(SimpleTestCase):
    def test_a_hop_to_a_proxy_in_the_project_passes(self):
        """
        Case: The check runs on the test project, where Review copies the email of auth.User through the proxy model
              catalog.Reviewer.
        Expected: No warning.
        """
        self.assertEqual(check_owners_outside_the_project(), [])


class IsolationLevelTestCase(SimpleTestCase):
    def run_check(self, isolation_level, check_level='warning'):
        """
        Run the check with a connection handler whose default database has ``isolation_level``, or no isolation level
        when it is None, and with ``ISOLATION_CHECK_LEVEL`` set to ``check_level``.
        """
        databases = copy.deepcopy(settings.DATABASES)
        databases['default']['OPTIONS'] = {} if isolation_level is None else {'isolation_level': isolation_level}
        with (
            override_settings(
                POSTGRES_DENORMALIZE={**settings.POSTGRES_DENORMALIZE, 'ISOLATION_CHECK_LEVEL': check_level}
            ),
            mock.patch('postgres_denormalize.checks.connections', ConnectionHandler(databases)),
        ):
            return check_isolation_level()

    def test_serializable_passes(self):
        """
        Case: The default database runs at SERIALIZABLE, as in the test settings.
        Expected: No message.
        """
        self.assertEqual(self.run_check(IsolationLevel.SERIALIZABLE), [])

    def test_the_default_isolation_level_is_a_warning(self):
        """
        Case: The default database sets no isolation level, so it runs at READ COMMITTED.
        Expected: One warning W303, which names the database and the level, and whose hint names SERIALIZABLE.
        """
        messages = self.run_check(None)

        self.assertEqual([message.id for message in messages], ['postgres_denormalize.W303'])
        self.assertIn("'default'", messages[0].msg)
        self.assertIn('READ COMMITTED', messages[0].msg)
        self.assertIn('IsolationLevel.SERIALIZABLE', messages[0].hint)

    def test_repeatable_read_is_reported_too(self):
        """
        Case: The default database runs at REPEATABLE READ, with ISOLATION_CHECK_LEVEL set to 'error'.
        Expected: One error E303, which names REPEATABLE READ.
        """
        messages = self.run_check(IsolationLevel.REPEATABLE_READ, 'error')

        self.assertEqual([message.id for message in messages], ['postgres_denormalize.E303'])
        self.assertIn('REPEATABLE READ', messages[0].msg)

    def test_the_check_can_be_turned_off(self):
        """
        Case: The default database runs at READ COMMITTED, with ISOLATION_CHECK_LEVEL set to 'off'.
        Expected: No message.
        """
        self.assertEqual(self.run_check(IsolationLevel.READ_COMMITTED, 'off'), [])
