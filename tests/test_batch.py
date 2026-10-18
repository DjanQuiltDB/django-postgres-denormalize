from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, connection
from django.db.models import Value
from django.db.models.functions import Concat
from django.test import TestCase, override_settings
from tenancy.models import Comment, Project, Task, Tenant

from postgres_denormalize.batch import defer_cascades, ignore_cascades
from postgres_denormalize.fields import drift_condition
from postgres_denormalize.triggers import DEFERRED_CASCADES_SETTING


def deferred_setting():
    with connection.cursor() as cursor:
        cursor.execute('SELECT current_setting(%s, true)', [DEFERRED_CASCADES_SETTING])
        return cursor.fetchone()[0]


class BatchHelperTests:
    """
    What both helpers promise: inside the block, the cascade triggers on the named owners do not run, and when the block
    ends, the copies below those owners are correct again.

    setUp creates the tenants Acme and Other, and the projects Apollo and Gemini of Acme. Each project has one task
    with one comment.
    """

    helper = None

    def setUp(self):
        self.tenant = Tenant.objects.create(name='Acme')
        self.other = Tenant.objects.create(name='Other')
        for name in ('Apollo', 'Gemini'):
            project = Project.objects.create(tenant=self.tenant, name=name)
            task = project.tasks.create(title=f'{name} launch')
            task.comments.create(body='Go')

    def project_names_on_tasks(self):
        return set(Task.objects.values_list('project_name', flat=True))

    def test_a_bulk_rename_skips_the_cascade_and_is_corrected_when_the_block_ends(self):
        """
        Case: Inside the helper's block for Project, one UPDATE renames every project.
        Expected: Inside the block, the tasks still have the old project names. After the block, they have the new
                  names.
        """
        with self.helper(Project):
            Project.objects.update(name=Concat('name', Value(' II')))

            self.assertEqual(self.project_names_on_tasks(), {'Apollo', 'Gemini'})

        self.assertEqual(self.project_names_on_tasks(), {'Apollo II', 'Gemini II'})

    def test_saving_a_task_in_the_block_does_not_correct_its_copy(self):
        """
        Case: Inside the helper's block for Project, one UPDATE renames every project, and then the task of Apollo is
              loaded and saved.
        Expected: Inside the block, the saved task still has the old project name, because the save writes the old
                  copy back unchanged. After the block, it has the new name.
        """
        with self.helper(Project):
            Project.objects.update(name=Concat('name', Value(' II')))
            task = Task.objects.get(title='Apollo launch')
            task.save()

            task.refresh_from_db()
            self.assertEqual(task.project_name, 'Apollo')

        task.refresh_from_db()
        self.assertEqual(task.project_name, 'Apollo II')

    def test_moving_projects_corrects_their_tasks_and_the_comments_below_them(self):
        """
        Case: Inside the helper's block for Project, one UPDATE moves every project from Acme to Other.
        Expected: Inside the block, the comments still have Acme as their tenant. After the block, every task and every
                  comment has Other as its tenant, and no task or comment differs from its lookup path.
        """
        with self.helper(Project):
            Project.objects.update(tenant=self.other)

            self.assertTrue(Comment.objects.filter(tenant=self.tenant).exists())

        self.assertEqual(set(Task.objects.values_list('tenant', flat=True)), {self.other.pk})
        self.assertEqual(set(Comment.objects.values_list('tenant', flat=True)), {self.other.pk})
        self.assertFalse(Task.objects.filter(drift_condition(Task)).exists())
        self.assertFalse(Comment.objects.filter(drift_condition(Comment)).exists())

    def test_an_exception_in_the_block_rolls_it_back_and_corrects_nothing(self):
        """
        Case: Inside the helper's block for Project, every project is renamed, and then the block raises a
              RuntimeError.
        Expected: The RuntimeError reaches the caller. The projects and the copies of their names on the tasks have
                  their old values.
        """
        with self.assertRaises(RuntimeError):
            with self.helper(Project):
                Project.objects.update(name='Gone')
                raise RuntimeError

        self.assertEqual(set(Project.objects.values_list('name', flat=True)), {'Apollo', 'Gemini'})
        self.assertEqual(self.project_names_on_tasks(), {'Apollo', 'Gemini'})

    def test_a_database_error_in_the_block_is_raised_unchanged(self):
        """
        Case: Inside the helper's block for Project, every project is renamed, and then a project without a tenant is
              inserted.
        Expected: The IntegrityError from the insert reaches the caller, and the tasks have their old project names.
        """
        with self.assertRaises(IntegrityError):
            with self.helper(Project):
                Project.objects.update(name='Gone')
                Project.objects.create(tenant_id=None, name='Orphan')

        self.assertEqual(self.project_names_on_tasks(), {'Apollo', 'Gemini'})

    def test_a_correction_that_breaks_a_constraint_of_the_leaf_rolls_the_batch_back(self):
        """
        Case: Other has a task with the title 'Apollo launch'. Inside the helper's block for Project, the projects of
              Acme move to Other, so the correction at the end gives Other two tasks with that title.
        Expected: An IntegrityError from the unique constraint on tasks, and Acme's task 'Apollo launch' still has Acme
                  as its tenant.
        """
        Project.objects.create(tenant=self.other, name='Mercury').tasks.create(title='Apollo launch')

        with self.assertRaises(IntegrityError):
            with self.helper(Project):
                Project.objects.filter(tenant=self.tenant).update(tenant=self.other)

        self.assertEqual(Task.objects.get(title='Apollo launch', project__name='Apollo').tenant, self.tenant)

    def test_cascades_run_again_once_the_block_ends(self):
        """
        Case: The helper's block for Project runs and ends without doing anything. After that, Apollo is renamed to
              Artemis.
        Expected: The cascade trigger runs for the rename, so the tasks have the names Artemis and Gemini.
        """
        with self.helper(Project):
            pass

        Project.objects.filter(name='Apollo').update(name='Artemis')

        self.assertEqual(self.project_names_on_tasks(), {'Artemis', 'Gemini'})

    def test_a_model_without_cascade_triggers_is_refused(self):
        """
        Case: The helper is called with Tenant, which has no cascade trigger, because no lookup path reads a column of
              Tenant after a first hop.
        Expected: A ValueError that names tenancy.Tenant.
        """
        with self.assertRaisesMessage(ValueError, 'tenancy.Tenant has no cascade trigger'):
            with self.helper(Tenant):
                pass

    def test_calling_without_a_model_is_refused(self):
        """
        Case: The helper is called without a model.
        Expected: A TypeError that asks for at least one model.
        """
        with self.assertRaisesMessage(TypeError, 'Name at least one model'):
            with self.helper():
                pass


class IgnoreCascadesTestCase(BatchHelperTests, TestCase):
    helper = staticmethod(ignore_cascades)


class DeferCascadesTestCase(BatchHelperTests, TestCase):
    helper = staticmethod(defer_cascades)

    def test_raw_sql_in_the_block_skips_the_cascade_too(self):
        """
        Case: Inside defer_cascades(Project), a raw SQL UPDATE renames every project.
        Expected: Inside the block, the tasks still have the old project names. After the block, they have the new
                  names.
        """
        with defer_cascades(Project):
            with connection.cursor() as cursor:
                cursor.execute("UPDATE tenancy_project SET name = name || ' II'")

            self.assertEqual(self.project_names_on_tasks(), {'Apollo', 'Gemini'})

        self.assertEqual(self.project_names_on_tasks(), {'Apollo II', 'Gemini II'})

    def test_only_the_cascade_triggers_on_the_named_owners_are_skipped(self):
        """
        Case: Inside defer_cascades(Project), the task 'Apollo launch' is moved to a new project of Other.
        Expected: The cascade trigger on the tasks table still runs, so inside the block the task's comment already
                  has Other as its tenant.
        """
        task = Task.objects.get(title='Apollo launch')

        with defer_cascades(Project):
            # The tasks table has the cascade trigger for the comments, and that trigger still runs.
            Task.objects.filter(pk=task.pk).update(project=Project.objects.create(tenant=self.other, name='Mercury'))

            self.assertEqual(task.comments.get().tenant, self.other)

    def test_a_nested_block_adds_its_triggers_and_restores_the_list_when_it_ends(self):
        """
        Case: Inside defer_cascades(Project), a nested defer_cascades(Task) block runs.
        Expected: The outer block lists the two cascade triggers on the projects table. The nested block lists those
                  two and the cascade trigger on the tasks table. After the nested block, the list is the outer block's
                  list again, and after the outer block, the setting is empty.
        """
        with defer_cascades(Project):
            outer = deferred_setting().split(',')
            with defer_cascades(Task):
                inner = deferred_setting().split(',')
            self.assertEqual(deferred_setting().split(','), outer)

        self.assertEqual(len(outer), 2)
        self.assertEqual(inner[:2], outer)
        self.assertEqual(len(inner), 3)
        self.assertEqual(deferred_setting(), '')

    @override_settings(POSTGRES_DENORMALIZE={'DEFERRABLE_CASCADES': False})
    def test_raises_when_deferrable_cascades_is_off(self):
        """
        Case: defer_cascades(Project) is called while DEFERRABLE_CASCADES is False.
        Expected: An ImproperlyConfigured error that points to ignore_cascades().
        """
        with self.assertRaisesMessage(ImproperlyConfigured, 'Use ignore_cascades() instead'):
            with defer_cascades(Project):
                pass
