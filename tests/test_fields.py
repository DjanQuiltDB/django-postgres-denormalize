import copy

from catalog.models import Category, Item, ItemAudit, Section
from django.apps import apps
from django.db import IntegrityError, connection, models, transaction
from django.db.models import F, Value
from django.db.models.functions import Concat
from django.test import SimpleTestCase, TestCase
from tenancy.models import Comment, Project, Task, Tenant

from postgres_denormalize.fields import drift_condition, get_denormalized_fields


def make_project(tenant=None, name='Apollo', code=None):
    return Project.objects.create(tenant=tenant or Tenant.objects.create(name='Acme'), name=name, code=code)


class WriteBackTestCase(TestCase):
    """
    A saved instance has the value that the trigger computes, without a reload. On an insert, the value comes back
    through RETURNING. On an update, it comes back through the expression that pre_save assigns.
    """

    def test_an_insert_reads_every_copy_back(self):
        """
        Case: A task is created in the project Apollo, whose code is APL.
        Expected: Without a reload, the instance has the project's tenant, name and code.
        """
        project = make_project(code='APL')

        task = Task.objects.create(project=project, title='Launch')

        self.assertEqual(task.tenant_id, project.tenant_id)
        self.assertEqual(task.tenant, project.tenant)
        self.assertEqual((task.project_name, task.project_code), ('Apollo', 'APL'))

    def test_a_null_source_value_is_copied_as_null(self):
        """
        Case: A task is created in a project without a code.
        Expected: The task's project_code is None.
        """
        task = Task.objects.create(project=make_project(code=None), title='Launch')

        self.assertIsNone(task.project_code)

    def test_a_value_that_code_assigns_is_overwritten_on_insert_and_update(self):
        """
        Case: A task is created with a tenant and a project_name that differ from its project's. Then the instance is
              assigned other values again and saved.
        Expected: After the insert, after the save and after a reload, the task has the project's tenant and name.
        """
        project = make_project()
        other = Tenant.objects.create(name='Other')

        task = Task.objects.create(project=project, title='Launch', tenant=other, project_name='made up')
        self.assertEqual((task.tenant_id, task.project_name), (project.tenant_id, 'Apollo'))

        task.tenant = other
        task.project_name = 'made up again'
        task.save()

        self.assertEqual((task.tenant_id, task.project_name), (project.tenant_id, 'Apollo'))
        task.refresh_from_db()
        self.assertEqual((task.tenant_id, task.project_name), (project.tenant_id, 'Apollo'))

    def test_moving_a_row_updates_the_instance_without_a_reload(self):
        """
        Case: A task is moved to the project Gemini of another tenant, and saved.
        Expected: Without a reload, the instance has the new tenant and the project name Gemini.
        """
        task = Task.objects.create(project=make_project(), title='Launch')
        self.assertIsNotNone(task.tenant)
        elsewhere = make_project(Tenant.objects.create(name='Elsewhere'), name='Gemini')

        task.project = elsewhere
        task.save()

        self.assertEqual((task.tenant, task.project_name), (elsewhere.tenant, 'Gemini'))

    def test_drift_condition_matches_nothing_while_the_triggers_are_in_place(self):
        """
        Case: A task and a comment are created while the triggers are installed.
        Expected: drift_condition matches no task and no comment.
        """
        Comment.objects.create(task=Task.objects.create(project=make_project(), title='Launch'), body='Go')

        self.assertFalse(Task.objects.filter(drift_condition(Task)).exists())
        self.assertFalse(Comment.objects.filter(drift_condition(Comment)).exists())


class InferredOnDeleteTestCase(TestCase):
    """
    Item.parent_category and Comment.tenant declare no on_delete, so they infer SET_NULL and CASCADE from their paths.
    """

    def test_deleting_the_parent_category_sets_the_copy_to_null_and_keeps_the_item(self):
        """
        Case: An item is in the category Bolts, whose parent is Fasteners. Fasteners is deleted.
        Expected: The item still exists, and its parent_category is None.
        """
        section = Section.objects.create(tenant=Tenant.objects.create(name='Acme'), title='Hardware')
        fasteners = Category.objects.create(section=section, label='Fasteners')
        bolts = Category.objects.create(section=section, parent=fasteners, label='Bolts')
        item = Item.objects.create(category=bolts, name='M6', home_section=section)
        self.assertEqual(item.parent_category, fasteners)

        fasteners.delete()

        item.refresh_from_db()
        self.assertIsNone(item.parent_category_id)

    def test_deleting_the_tenant_deletes_the_comments(self):
        """
        Case: A comment is on a task in a project of the tenant Acme. Acme is deleted.
        Expected: No comment exists.
        """
        comment = Comment.objects.create(task=Task.objects.create(project=make_project(), title='Launch'), body='Go')

        comment.tenant.delete()

        self.assertFalse(Comment.objects.exists())


class DriftConditionTestCase(TestCase):
    def test_a_model_without_denormalized_fields_matches_no_row(self):
        """
        Case: ItemAudit is excluded, so it has no denormalized fields, and it has one row. A tenant without copies has
              one row too.
        Expected: drift_condition matches no audit row and no tenant.
        """
        section = Section.objects.create(tenant=Tenant.objects.create(name='Acme'), title='Hardware')
        category = Category.objects.create(section=section, label='Bolts')
        item = Item.objects.create(category=category, name='M6', home_section=section)
        ItemAudit.objects.create(item=item, category=category, section_title='Hardware')

        self.assertFalse(ItemAudit.objects.filter(drift_condition(ItemAudit)).exists())
        self.assertFalse(Tenant.objects.filter(drift_condition(Tenant)).exists())


class TargetInferenceTestCase(SimpleTestCase):
    """
    A DenormalizedForeignKey without ``to`` infers its target from the lookup path, once every model along the path is
    loaded.
    """

    def test_the_target_is_the_model_that_the_last_hop_points_at(self):
        """
        Case: The tenant fields of Task and Comment are declared without ``to``.
        Expected: Their target is Tenant, the model that the last hop of each lookup path points at.
        """
        self.assertIs(Task._meta.get_field('tenant').related_model, Tenant)
        self.assertIs(Comment._meta.get_field('tenant').related_model, Tenant)

    def test_lazy_relative_and_cross_app_references_resolve(self):
        """
        Case: Item.tenant has a lookup path through models that are named by lazy and relative string references, and
              that ends in another app.
        Expected: Its target is Tenant.
        """
        self.assertIs(Item._meta.get_field('tenant').related_model, Tenant)

    def test_a_self_referential_hop_resolves_to_its_own_model(self):
        """
        Case: Item.parent_category has the lookup path category__parent, whose last hop points from Category to
              Category.
        Expected: Its target is Category.
        """
        self.assertIs(Item._meta.get_field('parent_category').related_model, Category)

    def test_an_explicit_target_is_kept(self):
        """
        Case: Item.section is declared with to='catalog.Section'.
        Expected: The field does not infer its target, and its target is Section.
        """
        field = Item._meta.get_field('section')

        self.assertFalse(field.target_from_path)
        self.assertIs(field.related_model, Section)

    def test_the_reverse_accessor_exists_on_the_inferred_target(self):
        """
        Case: Task.tenant and Item.parent_category declare a related_name and infer their target.
        Expected: Tenant has the reverse accessor tasks, and Category has grandchild_items.
        """
        self.assertTrue(hasattr(Tenant, 'tasks'))
        self.assertTrue(hasattr(Category, 'grandchild_items'))

    def test_deconstruct_records_the_inferred_target(self):
        """
        Case: Task.tenant is deconstructed.
        Expected: The keyword arguments have the inferred target and the lookup path, and do not have editable or
                  blank.
        """
        _name, _path, _args, kwargs = Task._meta.get_field('tenant').deconstruct()

        self.assertEqual(kwargs['to'], 'tenancy.tenant')
        self.assertEqual(kwargs['denormalize_from'], 'project__tenant')
        self.assertNotIn('editable', kwargs)
        self.assertNotIn('blank', kwargs)


class ColumnTypeTestCase(SimpleTestCase):
    def test_the_column_takes_the_type_of_its_source(self):
        """
        Case: Task.project_name copies Project.name, a CharField with max_length=100.
        Expected: The field has a CharField as its base field, and reports the database type, internal type, lookups
                  and form field of that CharField.
        """
        field = Task._meta.get_field('project_name')

        self.assertIsInstance(field.base_field, models.CharField)
        self.assertEqual(field.db_type(connection), 'varchar(100)')
        self.assertEqual(field.get_internal_type(), 'CharField')
        self.assertEqual(field.cast_db_type(connection), 'varchar(100)')
        self.assertEqual(field.db_parameters(connection)['type'], 'varchar(100)')
        self.assertFalse(field.null)
        self.assertTrue(field.empty_strings_allowed)
        self.assertIs(field.get_lookup('icontains'), models.CharField().get_lookup('icontains'))
        self.assertIsNone(field.get_transform('nonexistent'))
        self.assertIsNone(field.formfield())

    def test_a_nullable_source_makes_a_nullable_copy(self):
        """
        Case: Task.project_code copies Project.code, which is nullable.
        Expected: Task.project_code is nullable.
        """
        self.assertTrue(Task._meta.get_field('project_code').null)

    def test_values_convert_like_the_source(self):
        """
        Case: Task.project_name converts an integer and a string.
        Expected: The values are converted as a CharField converts them.
        """
        field = Task._meta.get_field('project_name')

        self.assertEqual(field.to_python(12), '12')
        self.assertEqual(field.get_prep_value('x'), 'x')
        self.assertEqual(field.get_db_prep_value('x', connection), 'x')

    def test_deconstruct_records_the_column_type(self):
        """
        Case: Task.project_name is deconstructed.
        Expected: The keyword arguments have a base field of CharField(max_length=100), and do not have null.
        """
        _name, _path, _args, kwargs = Task._meta.get_field('project_name').deconstruct()

        self.assertEqual(
            kwargs['base_field'].deconstruct()[1:], ('django.db.models.CharField', [], {'max_length': 100})
        )
        self.assertNotIn('null', kwargs)

    def test_an_excluded_model_has_no_denormalized_fields_but_keeps_the_column_type(self):
        """
        Case: ItemAudit is excluded, and has a DenormalizedField that copies Section.title, a CharField with
              max_length=80.
        Expected: get_denormalized_fields returns no fields for ItemAudit, and the column has the type varchar(80).
        """
        self.assertEqual(get_denormalized_fields(ItemAudit), [])
        self.assertEqual(ItemAudit._meta.get_field('section_title').db_type(connection), 'varchar(80)')


class FieldFlagsTestCase(SimpleTestCase):
    def test_every_denormalized_field_is_read_only_and_blank(self):
        """
        Case: Every denormalized field of the installed models is inspected.
        Expected: Every one of them is not editable and is blank.
        """
        fields = [field for model in apps.get_models() for field in get_denormalized_fields(model)]

        self.assertTrue(fields)
        self.assertEqual([str(field) for field in fields if field.editable or not field.blank], [])

    def test_a_different_lookup_path_does_not_alter_the_column(self):
        """
        Case: A copy of Task.tenant gets a different lookup path, and the schema editor compares it with the original.
        Expected: The schema editor does not alter the column.
        """
        old = Task._meta.get_field('tenant')
        # A shallow copy keeps the resolved relation. clone() would turn it back into a lazy reference.
        new = copy.copy(old)
        new.denormalize_from = 'project__other_tenant'

        self.assertFalse(connection.SchemaEditorClass(connection)._field_should_be_altered(old, new))

    def test_the_installed_models_pass_their_field_checks(self):
        """
        Case: The checks of every field of the installed models run.
        Expected: No errors.
        """
        errors = [error for model in apps.get_models() for field in model._meta.fields for error in field.check()]

        self.assertEqual(errors, [])


class QueryTestCase(TestCase):
    def test_the_copy_combines_with_other_text_without_an_output_field(self):
        """
        Case: A query annotates tasks with Concat of project_name, a literal and title, without an output_field.
        Expected: The annotation is 'Apollo: Launch'.
        """
        task = Task.objects.create(project=make_project(), title='Launch')

        label = (
            Task.objects.filter(pk=task.pk)
            .annotate(label=Concat(F('project_name'), Value(': '), F('title')))
            .values_list('label', flat=True)
            .get()
        )

        self.assertEqual(label, 'Apollo: Launch')

    def test_lookups_and_ordering_work_on_the_copy(self):
        """
        Case: Tasks of the projects Beta and Alpha are created in that order, then listed in the default ordering, and
              filtered with project_name__icontains='ALP'.
        Expected: The listing has Alpha before Beta, and the filter matches one task.
        """
        tenant = Tenant.objects.create(name='Acme')
        Task.objects.create(project=make_project(tenant, name='Beta'), title='b')
        Task.objects.create(project=make_project(tenant, name='Alpha'), title='a')

        self.assertEqual(list(Task.objects.values_list('project_name', flat=True)), ['Alpha', 'Beta'])
        self.assertEqual(Task.objects.filter(project_name__icontains='ALP').count(), 1)

    def test_the_unique_constraint_that_includes_the_copy_is_enforced(self):
        """
        Case: A tenant has a task with the title Launch. Another task with the title Launch is created in a second
              project of the same tenant, and then in a project of another tenant.
        Expected: The first insert raises an IntegrityError, and the second succeeds.
        """
        tenant = Tenant.objects.create(name='Acme')
        Task.objects.create(project=make_project(tenant), title='Launch')

        with self.assertRaises(IntegrityError), transaction.atomic():
            Task.objects.create(project=make_project(tenant, name='Gemini'), title='Launch')

        Task.objects.create(project=make_project(), title='Launch')
