from catalog.models import ItemAudit
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from tenancy.models import Task

from postgres_denormalize import conf


class GetSettingTestCase(SimpleTestCase):
    @override_settings(POSTGRES_DENORMALIZE={})
    def test_every_key_falls_back_to_its_default(self):
        """
        Case: POSTGRES_DENORMALIZE is an empty dict, and every key is read.
        Expected: Each key has its default value.
        """
        self.assertEqual(conf.get_setting('EXCLUDE_MODELS'), ())
        self.assertIsNone(conf.get_setting('EXCLUDE_MODELS_CALLBACK'))
        self.assertEqual(
            conf.get_setting('EXCLUDE_MODELS_SILENCED_CHECKS'),
            (
                'postgres_denormalize.E002',
                'postgres_denormalize.E003',
                'postgres_denormalize.E004',
                'postgres_denormalize.W009',
            ),
        )
        self.assertEqual(conf.get_setting('ORDERING_CHECK_LEVEL'), 'warning')
        self.assertEqual(conf.get_setting('ISOLATION_CHECK_LEVEL'), 'warning')
        self.assertEqual(conf.get_setting('REDUNDANT_COPY_CHECK'), conf.DEFAULT_REDUNDANT_COPY_CHECK)
        self.assertIs(conf.get_setting('DEFERRABLE_CASCADES'), True)

    @override_settings(POSTGRES_DENORMALIZE={'REDUNDANT_COPY_CHECK': {'LEVEL': 'off'}})
    def test_the_redundant_copy_check_is_merged_key_by_key(self):
        """
        Case: REDUNDANT_COPY_CHECK sets only LEVEL to 'off'.
        Expected: LEVEL is 'off', and MAX_DEPTH has its default value of 4.
        """
        merged = conf.get_setting('REDUNDANT_COPY_CHECK')

        self.assertEqual(merged['LEVEL'], 'off')
        self.assertEqual(merged['MAX_DEPTH'], 4)

    @override_settings()
    def test_a_project_without_the_setting_gets_the_defaults(self):
        """
        Case: The project has no POSTGRES_DENORMALIZE setting.
        Expected: The project's setting reads as an empty dict, and Task is not excluded.
        """
        from django.conf import settings

        del settings.POSTGRES_DENORMALIZE

        self.assertEqual(conf.user_settings(), {})
        self.assertFalse(conf.is_excluded(Task))


def exclude_tasks(model):
    return model._meta.model_name == 'task'


class IsExcludedTestCase(SimpleTestCase):
    def test_a_listed_label_excludes_its_model_in_any_letter_case(self):
        """
        Case: EXCLUDE_MODELS lists catalog.ItemAudit as in the test settings, and then as 'CATALOG.itemaudit'.
        Expected: ItemAudit is excluded with either spelling, and Task is not excluded.
        """
        self.assertTrue(conf.is_excluded(ItemAudit))
        self.assertFalse(conf.is_excluded(Task))

        with override_settings(POSTGRES_DENORMALIZE={'EXCLUDE_MODELS': ['CATALOG.itemaudit']}):
            self.assertTrue(conf.is_excluded(ItemAudit))

    @override_settings(POSTGRES_DENORMALIZE={'EXCLUDE_MODELS_CALLBACK': 'catalog.exclusions.is_audit_model'})
    def test_a_callback_given_as_a_dotted_path_decides(self):
        """
        Case: EXCLUDE_MODELS_CALLBACK is the dotted path of a callback that excludes models whose name ends in audit.
        Expected: ItemAudit is excluded, and Task is not.
        """
        self.assertTrue(conf.is_excluded(ItemAudit))
        self.assertFalse(conf.is_excluded(Task))

    @override_settings(POSTGRES_DENORMALIZE={'EXCLUDE_MODELS_CALLBACK': exclude_tasks})
    def test_a_callback_given_as_a_callable_decides(self):
        """
        Case: EXCLUDE_MODELS_CALLBACK is a function that excludes Task.
        Expected: Task is excluded, and ItemAudit is not.
        """
        self.assertTrue(conf.is_excluded(Task))
        self.assertFalse(conf.is_excluded(ItemAudit))

    @override_settings(
        POSTGRES_DENORMALIZE={'EXCLUDE_MODELS': ['catalog.ItemAudit'], 'EXCLUDE_MODELS_CALLBACK': exclude_tasks}
    )
    def test_either_one_is_enough(self):
        """
        Case: EXCLUDE_MODELS lists catalog.ItemAudit, and EXCLUDE_MODELS_CALLBACK is a function that excludes Task.
        Expected: Both ItemAudit and Task are excluded.
        """
        self.assertTrue(conf.is_excluded(ItemAudit))
        self.assertTrue(conf.is_excluded(Task))

    @override_settings(POSTGRES_DENORMALIZE={})
    def test_no_model_is_excluded_without_either(self):
        """
        Case: Neither EXCLUDE_MODELS nor EXCLUDE_MODELS_CALLBACK is set.
        Expected: ItemAudit is not excluded.
        """
        self.assertFalse(conf.is_excluded(ItemAudit))


class ValidateSettingsTestCase(SimpleTestCase):
    def assertRefused(self, setting, fragment):
        with override_settings(POSTGRES_DENORMALIZE=setting):
            with self.assertRaisesMessage(ImproperlyConfigured, fragment):
                conf.validate_settings()

    def test_the_test_project_setting_is_valid(self):
        """
        Case: The setting of the test project is validated.
        Expected: No error.
        """
        conf.validate_settings()

    def test_refuses_a_setting_that_is_not_a_dict(self):
        """
        Case: POSTGRES_DENORMALIZE is a list.
        Expected: An ImproperlyConfigured error that says that the setting must be a dict.
        """
        self.assertRefused(['EXCLUDE_MODELS'], 'must be a dict')

    def test_refuses_an_unknown_key(self):
        """
        Case: POSTGRES_DENORMALIZE has the unknown key EXCLUDE.
        Expected: An ImproperlyConfigured error that names the key.
        """
        self.assertRefused({'EXCLUDE': None}, 'unknown keys: EXCLUDE')

    def test_refuses_excluded_models_that_are_not_a_list_of_labels(self):
        """
        Case: EXCLUDE_MODELS is a string, and then a list with the entry 'ItemAudit', which is not a model label.
        Expected: For the string, an ImproperlyConfigured error that asks for a list of model labels. For the list, an
                  ImproperlyConfigured error that names the entry.
        """
        self.assertRefused({'EXCLUDE_MODELS': 'catalog.ItemAudit'}, 'must be a list of model labels')
        self.assertRefused({'EXCLUDE_MODELS': ['ItemAudit']}, "contains 'ItemAudit'")

    def test_refuses_a_callback_that_cannot_be_imported(self):
        """
        Case: EXCLUDE_MODELS_CALLBACK is a dotted path to a function that does not exist.
        Expected: An ImproperlyConfigured error that says that the path cannot be imported.
        """
        self.assertRefused({'EXCLUDE_MODELS_CALLBACK': 'catalog.exclusions.missing'}, 'cannot be imported')

    def test_refuses_a_callback_that_is_not_callable(self):
        """
        Case: EXCLUDE_MODELS_CALLBACK is the integer 42.
        Expected: An ImproperlyConfigured error that asks for a callable or a dotted path.
        """
        self.assertRefused({'EXCLUDE_MODELS_CALLBACK': 42}, 'must be a callable or a dotted path')

    @override_settings(POSTGRES_DENORMALIZE={'EXCLUDE_MODELS_CALLBACK': exclude_tasks})
    def test_accepts_a_callable_callback(self):
        """
        Case: EXCLUDE_MODELS_CALLBACK is a function.
        Expected: No error.
        """
        conf.validate_settings()

    def test_refuses_silenced_checks_that_are_not_a_list_of_field_checks(self):
        """
        Case: EXCLUDE_MODELS_SILENCED_CHECKS is a string, and then a list with the project check W101 and the
              misspelt 'postgres_denormalize.W9'.
        Expected: For the string, an ImproperlyConfigured error that asks for a list of check ids. For the list, an
                  ImproperlyConfigured error that names both entries and the field checks that can be listed.
        """
        self.assertRefused(
            {'EXCLUDE_MODELS_SILENCED_CHECKS': 'postgres_denormalize.W009'}, 'must be a list of check ids'
        )
        with override_settings(
            POSTGRES_DENORMALIZE={
                'EXCLUDE_MODELS_SILENCED_CHECKS': ['postgres_denormalize.W101', 'postgres_denormalize.W9']
            }
        ):
            with self.assertRaises(ImproperlyConfigured) as raised:
                conf.validate_settings()
        message = str(raised.exception)
        self.assertIn("'postgres_denormalize.W101', 'postgres_denormalize.W9'", message)
        self.assertIn('postgres_denormalize.E001', message)
        self.assertIn('postgres_denormalize.W009', message)

    def test_accepts_silenced_checks_from_none_to_every_field_check(self):
        """
        Case: EXCLUDE_MODELS_SILENCED_CHECKS is an empty list, and then a list of every field check.
        Expected: No error for either.
        """
        for silenced in ([], list(conf.FIELD_CHECKS)):
            with (
                self.subTest(silenced=silenced),
                override_settings(POSTGRES_DENORMALIZE={'EXCLUDE_MODELS_SILENCED_CHECKS': silenced}),
            ):
                conf.validate_settings()

    def test_refuses_a_redundant_copy_check_that_is_not_a_dict(self):
        """
        Case: REDUNDANT_COPY_CHECK is the string 'error'.
        Expected: An ImproperlyConfigured error that says that REDUNDANT_COPY_CHECK must be a dict.
        """
        self.assertRefused({'REDUNDANT_COPY_CHECK': 'error'}, '["REDUNDANT_COPY_CHECK"] must be a dict')

    def test_refuses_an_unknown_redundant_copy_check_key(self):
        """
        Case: REDUNDANT_COPY_CHECK has the unknown key LEVLE.
        Expected: An ImproperlyConfigured error that names the key.
        """
        self.assertRefused({'REDUNDANT_COPY_CHECK': {'LEVLE': 'error'}}, 'unknown keys: LEVLE')

    def test_refuses_an_unknown_level(self):
        """
        Case: The LEVEL of REDUNDANT_COPY_CHECK is 'loud', then ORDERING_CHECK_LEVEL is 'quiet', and then
              ISOLATION_CHECK_LEVEL is 'strict'.
        Expected: An ImproperlyConfigured error that names the level, for each of them.
        """
        self.assertRefused({'REDUNDANT_COPY_CHECK': {'LEVEL': 'loud'}}, "is 'loud'")
        self.assertRefused({'ORDERING_CHECK_LEVEL': 'quiet'}, "is 'quiet'")
        self.assertRefused({'ISOLATION_CHECK_LEVEL': 'strict'}, "is 'strict'")

    def test_refuses_apps_that_are_not_a_list(self):
        """
        Case: The APPS of REDUNDANT_COPY_CHECK is the string 'tenancy'.
        Expected: An ImproperlyConfigured error that asks for a list of labels.
        """
        self.assertRefused({'REDUNDANT_COPY_CHECK': {'APPS': 'tenancy'}}, 'list of labels')

    def test_refuses_allowed_entries_that_are_not_a_dict(self):
        """
        Case: The ALLOWED of REDUNDANT_COPY_CHECK is a list.
        Expected: An ImproperlyConfigured error that says that ALLOWED must be a dict.
        """
        self.assertRefused(
            {'REDUNDANT_COPY_CHECK': {'ALLOWED': ['tenancy.Task.project']}}, '["ALLOWED"] must be a dict'
        )

    def test_refuses_a_depth_below_two(self):
        """
        Case: The MAX_DEPTH of REDUNDANT_COPY_CHECK is 1.
        Expected: An ImproperlyConfigured error that asks for an integer of 2 or more.
        """
        self.assertRefused({'REDUNDANT_COPY_CHECK': {'MAX_DEPTH': 1}}, 'integer of 2 or more')

    def test_refuses_deferrable_cascades_that_is_not_a_bool(self):
        """
        Case: DEFERRABLE_CASCADES is the string 'off', and then the integer 0.
        Expected: An ImproperlyConfigured error that asks for True or False, for each of them.
        """
        self.assertRefused({'DEFERRABLE_CASCADES': 'off'}, '["DEFERRABLE_CASCADES"] must be True or False')
        self.assertRefused({'DEFERRABLE_CASCADES': 0}, '["DEFERRABLE_CASCADES"] must be True or False')

    @override_settings(POSTGRES_DENORMALIZE={'DEFERRABLE_CASCADES': False})
    def test_accepts_deferrable_cascades_turned_off(self):
        """
        Case: DEFERRABLE_CASCADES is False.
        Expected: No error.
        """
        conf.validate_settings()
