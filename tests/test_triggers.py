import pgtrigger
from catalog.models import Category, Item, ItemAudit, Review, Reviewer, Section
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase
from tenancy.models import Comment, Project, Task, Tenant

from postgres_denormalize.triggers import (
    MAX_TRIGGER_NAME_LENGTH,
    DenormalizeCascadeTrigger,
    DenormalizeFillTrigger,
    DenormalizeSetTrigger,
    build_cascade_update_sql,
    cascade_trigger_name,
    register_triggers,
)


def trigger_names(model, trigger_class):
    return sorted(
        trigger.name for trigger in getattr(model._meta, 'triggers', []) if isinstance(trigger, trigger_class)
    )


def row_version(model, pk):
    """
    Return the row's xmin. Postgres changes it whenever the row is written, so a test can tell a rewritten row apart
    from a row that was not written.
    """
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT xmin::text FROM {model._meta.db_table} WHERE id = %s', [pk])
        return cursor.fetchone()[0]


class SetTriggerSqlTestCase(SimpleTestCase):
    def test_the_lookup_reads_each_ancestor_row_once(self):
        """
        Case: The function of the set trigger on Task is compiled. Task copies three columns of its project.
        Expected: One early return for unchanged rows, then one SELECT that reads all three columns from the project.
        """
        self.assertEqual(
            DenormalizeSetTrigger().get_func(Task),
            "IF TG_OP = 'UPDATE' THEN\n"
            '    IF OLD."project_id" IS NOT DISTINCT FROM NEW."project_id"\n'
            '        AND OLD."tenant_id" IS NOT DISTINCT FROM NEW."tenant_id"\n'
            '        AND OLD."project_name" IS NOT DISTINCT FROM NEW."project_name"\n'
            '        AND OLD."project_code" IS NOT DISTINCT FROM NEW."project_code" THEN\n'
            '        RETURN NEW;\n'
            '    END IF;\n'
            'END IF;\n'
            'SELECT hop0."tenant_id" AS value0, hop0."name" AS value1, hop0."code" AS value2\n'
            'INTO ancestor0\n'
            'FROM "tenancy_project" hop0\n'
            'WHERE hop0."id" = NEW."project_id";\n'
            'IF FOUND THEN\n'
            '    NEW."tenant_id" := ancestor0.value0;\n'
            '    NEW."project_name" := ancestor0.value1;\n'
            '    NEW."project_code" := ancestor0.value2;\n'
            'END IF;\n'
            'RETURN NEW;',
        )

    def test_intermediate_hops_are_left_joined(self):
        """
        Case: The function of the set trigger on Comment is compiled. Its lookup path crosses tasks and projects.
        Expected: The projects table is joined to the tasks table with a LEFT JOIN.
        """
        func = DenormalizeSetTrigger().get_func(Comment)

        self.assertIn(
            'FROM "tenancy_task" hop0\nLEFT JOIN "tenancy_project" hop1 ON hop1."id" = hop0."project_id"\n', func
        )

    def test_one_record_is_declared_per_ancestor_row(self):
        """
        Case: The declarations of the set triggers on Task and Item are compiled. Task reads one ancestor row, and Item
              reads two.
        Expected: One record variable for Task and two for Item, and no %TYPE in the function of Item.
        """
        trigger = DenormalizeSetTrigger()

        self.assertEqual(trigger.get_declare(Task), [('ancestor0', 'record')])
        self.assertEqual(trigger.get_declare(Item), [('ancestor0', 'record'), ('ancestor1', 'record')])
        self.assertNotIn('%TYPE', trigger.get_func(Item))


class CascadeTriggerSqlTestCase(SimpleTestCase):
    def test_copies_below_the_same_row_are_rewritten_in_one_statement(self):
        """
        Case: The cascade statement for the tenant and project_name of Task is built for the projects table.
        Expected: One UPDATE that sets both copies and only writes rows in which one of them differs.
        """
        tenant = Task._meta.get_field('tenant')
        name = Task._meta.get_field('project_name')

        self.assertEqual(
            build_cascade_update_sql(Task, [(tenant, tenant.resolve_chain(), 1), (name, name.resolve_chain(), 1)]),
            'UPDATE "tenancy_task" leaf\n'
            'SET "tenant_id" = NEW."tenant_id",\n'
            '    "project_name" = NEW."name"\n'
            'WHERE leaf."project_id" = NEW."id"\n'
            '  AND (leaf."tenant_id" IS DISTINCT FROM NEW."tenant_id"\n'
            '    OR leaf."project_name" IS DISTINCT FROM NEW."name");',
        )

    def test_a_moved_second_hop_walks_the_rest_of_the_path_from_the_new_row(self):
        """
        Case: The cascade statement for the tenant of Comment is built for the tasks table, where the second hop of the
              path, project, can change.
        Expected: The new tenant is read with a subquery that walks from the task's new project to its tenant.
        """
        value = '(SELECT upper0."tenant_id"\n FROM "tenancy_project" upper0\n WHERE upper0."id" = NEW."project_id")'
        tenant = Comment._meta.get_field('tenant')

        self.assertEqual(
            build_cascade_update_sql(Comment, [(tenant, tenant.resolve_chain(), 1)]),
            'UPDATE "tenancy_comment" leaf\n'
            f'SET "tenant_id" = {value}\n'
            'WHERE leaf."task_id" = NEW."id"\n'
            f'  AND leaf."tenant_id" IS DISTINCT FROM {value};',
        )

    def test_a_changed_last_hop_reaches_the_leaves_through_the_hops_between(self):
        """
        Case: The cascade statement for the tenant of Comment is built for the projects table, where the last hop of the
              path, tenant, can change.
        Expected: The UPDATE finds the comments through the tasks table in between.
        """
        tenant = Comment._meta.get_field('tenant')

        self.assertEqual(
            build_cascade_update_sql(Comment, [(tenant, tenant.resolve_chain(), 2)]),
            'UPDATE "tenancy_comment" leaf\n'
            'SET "tenant_id" = NEW."tenant_id"\n'
            'FROM "tenancy_task" hop0\n'
            'WHERE leaf."task_id" = hop0."id"\n'
            '  AND hop0."project_id" = NEW."id"\n'
            '  AND leaf."tenant_id" IS DISTINCT FROM NEW."tenant_id";',
        )

    def test_the_condition_tests_every_copied_column_of_the_table(self):
        """
        Case: The condition of the cascade trigger for Task is compiled for Project.
        Expected: The condition tests the three columns of Project that Task copies. The function ends with RETURN NULL.
        """
        trigger = DenormalizeCascadeTrigger(leaf_label='tenancy.Task')

        self.assertEqual(
            trigger.get_condition(Project).resolve(Project),
            'OLD."tenant_id" IS DISTINCT FROM NEW."tenant_id" OR OLD."name" IS DISTINCT FROM NEW."name" '
            'OR OLD."code" IS DISTINCT FROM NEW."code"',
        )
        self.assertTrue(trigger.get_func(Project).endswith('RETURN NULL;'))


class FillTriggerSqlTestCase(SimpleTestCase):
    def test_the_fill_trigger_runs_the_cascade_statements_without_a_condition(self):
        """
        Case: The fill trigger for Comment is compiled for Project. Comment reaches Project through Task.
        Expected: The trigger fires after an INSERT, has no condition, and its function is the cascade statement for
                  Project followed by RETURN NULL.
        """
        trigger = DenormalizeFillTrigger(leaf_label='tenancy.Comment')
        tenant = Comment._meta.get_field('tenant')

        self.assertEqual(trigger.operation, pgtrigger.Insert)
        self.assertIsNone(trigger.get_condition(Project))
        self.assertEqual(
            trigger.get_func(Project),
            build_cascade_update_sql(Comment, [(tenant, tenant.resolve_chain(), 2)]) + '\nRETURN NULL;',
        )


class TriggerNameTestCase(SimpleTestCase):
    def test_a_short_label_is_spelled_out(self):
        """
        Case: The cascade trigger for the leaf tenancy.Task is named.
        Expected: The name is denormalize_cascade_tenancy_task.
        """
        self.assertEqual(cascade_trigger_name('tenancy.Task'), 'denormalize_cascade_tenancy_task')

    def test_a_long_label_is_cut_and_hashed_within_the_limit(self):
        """
        Case: The cascade trigger for a leaf with a long label is named twice, and the trigger for a label that differs
              only in its last letter is named once.
        Expected: The name fits pgtrigger's limit, starts with the prefix and the app label, is the same both times,
                  and differs from the name for the other label.
        """
        long_label = 'inventory_management.WarehouseStockReservationLine'

        name = cascade_trigger_name(long_label)

        self.assertLessEqual(len(name), MAX_TRIGGER_NAME_LENGTH)
        self.assertTrue(name.startswith('denormalize_cascade_inventory'))
        self.assertEqual(name, cascade_trigger_name(long_label))
        self.assertNotEqual(name, cascade_trigger_name(f'{long_label}s'))

    def test_labels_that_join_to_the_same_name_get_different_names(self):
        """
        Case: The cascade triggers for the leaves a_b.C and a.B_C are named. Both join to denormalize_cascade_a_b_c.
        Expected: The two names differ, the name for a.B_C is spelled out, and the name for a_b.C, whose app label
                  contains an underscore, has a hash.
        """
        self.assertEqual(cascade_trigger_name('a.B_C'), 'denormalize_cascade_a_b_c')
        self.assertRegex(cascade_trigger_name('a_b.C'), r'^denormalize_cascade_a_b_c_[0-9a-f]{8}$')

    def test_the_fill_trigger_has_its_own_prefix(self):
        """
        Case: The fill trigger for the leaf tenancy.Task is named.
        Expected: The name is denormalize_fill_tenancy_task.
        """
        self.assertEqual(DenormalizeFillTrigger(leaf_label='tenancy.Task').name, 'denormalize_fill_tenancy_task')


class RegistrationTestCase(SimpleTestCase):
    def test_every_leaf_gets_the_set_trigger(self):
        """
        Case: The triggers of the leaves Task, Comment and Item are listed.
        Expected: Each leaf has one set trigger, named denormalize_set.
        """
        for model in (Task, Comment, Item):
            with self.subTest(model=model.__name__):
                self.assertEqual(trigger_names(model, DenormalizeSetTrigger), ['denormalize_set'])

    def test_every_model_that_a_path_crosses_gets_one_cascade_trigger_per_leaf(self):
        """
        Case: The cascade triggers of Project, Task, Category, Section and Tenant are listed.
        Expected: Project has one for Comment and one for Task. Task has one for Comment. Category and Section each have
                  one for Item. Tenant, which no lookup path crosses after a first hop, has none.
        """
        self.assertEqual(
            trigger_names(Project, DenormalizeCascadeTrigger),
            ['denormalize_cascade_tenancy_comment', 'denormalize_cascade_tenancy_task'],
        )
        self.assertEqual(trigger_names(Task, DenormalizeCascadeTrigger), ['denormalize_cascade_tenancy_comment'])
        self.assertEqual(trigger_names(Category, DenormalizeCascadeTrigger), ['denormalize_cascade_catalog_item'])
        self.assertEqual(trigger_names(Section, DenormalizeCascadeTrigger), ['denormalize_cascade_catalog_item'])
        self.assertEqual(trigger_names(Tenant, DenormalizeCascadeTrigger), [])

    def test_every_model_with_a_cascade_trigger_has_a_fill_trigger_for_the_same_leaf(self):
        """
        Case: The fill triggers of Project, Task, Category, Section, Reviewer and Tenant are listed.
        Expected: Each model has one fill trigger for each leaf that it has a cascade trigger for, and Tenant has none.
        """
        for model in (Project, Task, Category, Section, Reviewer, Tenant):
            with self.subTest(model=model.__name__):
                self.assertEqual(
                    trigger_names(model, DenormalizeFillTrigger),
                    [
                        name.replace('denormalize_cascade_', 'denormalize_fill_')
                        for name in trigger_names(model, DenormalizeCascadeTrigger)
                    ],
                )

    def test_a_hop_to_a_proxy_model_registers_the_triggers_on_the_proxy(self):
        """
        Case: The triggers of catalog.Reviewer, a proxy model of auth.User, and of auth.User are listed. Review points
              at Reviewer and copies reviewer__email.
        Expected: Reviewer has the cascade trigger and the fill trigger for Review. auth.User has neither.
        """
        self.assertEqual(trigger_names(Reviewer, DenormalizeCascadeTrigger), ['denormalize_cascade_catalog_review'])
        self.assertEqual(trigger_names(Reviewer, DenormalizeFillTrigger), ['denormalize_fill_catalog_review'])
        self.assertEqual(trigger_names(User, DenormalizeCascadeTrigger), [])
        self.assertEqual(trigger_names(User, DenormalizeFillTrigger), [])

    def test_an_excluded_model_gets_no_trigger(self):
        """
        Case: The set triggers of ItemAudit, which is excluded, are listed.
        Expected: ItemAudit has no set trigger.
        """
        self.assertEqual(trigger_names(ItemAudit, DenormalizeSetTrigger), [])

    def test_registering_again_changes_nothing(self):
        """
        Case: register_triggers runs twice more after the app registry has registered the triggers.
        Expected: Every model has the same triggers as before.
        """
        models = [Task, Project, Comment, Item, Category, Section, Review, Reviewer]
        before = {model: [trigger.name for trigger in model._meta.triggers] for model in models}

        register_triggers()
        register_triggers()

        self.assertEqual({model: [trigger.name for trigger in model._meta.triggers] for model in models}, before)


class CascadeTestCase(TestCase):
    """
    Changing a column that a lookup path reads rewrites every copy below it, and only the copies that change.

    setUp creates the tenants Acme and Other, and the project Apollo of Acme with the code APL. Apollo has the task
    Launch with one comment, and the archived task Plan.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name='Acme')
        self.other = Tenant.objects.create(name='Other')
        self.project = Project.objects.create(tenant=self.tenant, name='Apollo', code='APL')
        self.task = Task.objects.create(project=self.project, title='Launch')
        self.archived = Task.objects.create(project=self.project, title='Plan', archived=True)
        self.comment = Comment.objects.create(task=self.task, body='Go')

    def test_moving_a_project_rewrites_the_tenant_of_its_tasks_and_their_comments(self):
        """
        Case: Apollo moves to Other.
        Expected: The task Launch and its comment have Other as their tenant.
        """
        self.project.tenant = self.other
        self.project.save()

        self.assertEqual(Task.objects.get(pk=self.task.pk).tenant_id, self.other.pk)
        self.assertEqual(Comment.objects.get(pk=self.comment.pk).tenant_id, self.other.pk)

    def test_renaming_a_project_renames_every_copy_including_archived_rows(self):
        """
        Case: Apollo is renamed to Artemis.
        Expected: Both tasks, including the archived one, have the project name Artemis.
        """
        self.project.name = 'Artemis'
        self.project.save()

        self.assertEqual(set(Task.objects.values_list('project_name', flat=True)), {'Artemis'})

    def test_moving_a_task_rewrites_the_tenant_of_its_comments(self):
        """
        Case: The task Launch moves to the project Gemini of Other.
        Expected: The comment of the task has Other as its tenant.
        """
        elsewhere = Project.objects.create(tenant=self.other, name='Gemini')

        self.task.project = elsewhere
        self.task.save()

        self.assertEqual(Comment.objects.get(pk=self.comment.pk).tenant_id, self.other.pk)

    def test_one_statement_moving_many_rows_reconciles_all_of_them(self):
        """
        Case: Acme also has the project Gemini with the task Orbit. One UPDATE moves every project of Acme to Other.
        Expected: No task has Acme as its tenant.
        """
        Project.objects.create(tenant=self.tenant, name='Gemini').tasks.create(title='Orbit')

        Project.objects.filter(tenant=self.tenant).update(tenant=self.other)

        self.assertFalse(Task.objects.filter(tenant=self.tenant).exists())

    def test_an_update_that_changes_no_copied_column_writes_no_leaf(self):
        """
        Case: Apollo's code is updated to its current value, and then the project is saved without changes.
        Expected: The task Launch is not written.
        """
        before = row_version(Task, self.task.pk)

        Project.objects.filter(pk=self.project.pk).update(code='APL')
        self.project.save()

        self.assertEqual(row_version(Task, self.task.pk), before)

    def test_moving_a_task_within_its_tenant_leaves_its_comments_unchanged(self):
        """
        Case: The task Launch moves to the project Gemini, which also belongs to Acme.
        Expected: The comment of the task is not written.
        """
        before = row_version(Comment, self.comment.pk)

        self.task.project = Project.objects.create(tenant=self.tenant, name='Gemini')
        self.task.save()

        self.assertEqual(row_version(Comment, self.comment.pk), before)

    def test_a_move_onto_a_title_already_in_use_is_refused_by_the_leafs_constraint(self):
        """
        Case: Other has a task with the title Launch, and Apollo, which also has a task Launch, moves to Other.
        Expected: An IntegrityError from the unique constraint on tasks.
        """
        Project.objects.create(tenant=self.other, name='Gemini').tasks.create(title='Launch')

        self.project.tenant = self.other
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.project.save()

    def test_a_nulled_hop_and_a_moved_self_referential_hop_cascade(self):
        """
        Case: The item M6 is in the category Bolts, whose parent is Root. Bolts loses its parent, and the section of
              Bolts is renamed to Fasteners and moves to Other.
        Expected: The item has no parent category, and has the section title Fasteners, the tenant Other and the same
                  section.
        """
        section = Section.objects.create(tenant=self.tenant, title='Hardware')
        root = Category.objects.create(section=section, label='Root')
        category = Category.objects.create(section=section, label='Bolts', parent=root)
        item = Item.objects.create(category=category, name='M6', home_section=section)
        self.assertEqual(item.parent_category, root)

        category.parent = None
        category.save()
        section.title = 'Fasteners'
        section.tenant = self.other
        section.save()

        item.refresh_from_db()
        self.assertIsNone(item.parent_category_id)
        self.assertEqual(
            (item.section_title, item.tenant_id, item.section_id), ('Fasteners', self.other.pk, section.pk)
        )

    def test_a_change_through_the_concrete_model_of_a_proxy_owner_cascades(self):
        """
        Case: The reviewer Ann, with the email ann@example.com, has written a review. Her email is changed through
              auth.User, the concrete model of the proxy Reviewer that Review points at.
        Expected: The review has the new email.
        """
        user = User.objects.create(username='ann', email='ann@example.com')
        review = Review.objects.create(reviewer_id=user.pk, body='Fine')

        user.email = 'ann@example.org'
        user.save()

        review.refresh_from_db()
        self.assertEqual(review.reviewer_email, 'ann@example.org')


class SetTriggerTestCase(TestCase):
    def test_a_bulk_load_inserting_a_leaf_before_its_ancestor_keeps_the_value_that_the_load_supplies(self):
        """
        Case: Like a fixture load, raw SQL inserts a task whose project does not exist yet, with a tenant and project
              name of its own. Then the project is inserted.
        Expected: The task keeps the tenant and project name that the insert supplied.
        """
        tenant = Tenant.objects.create(name='Acme')

        # Django declares foreign keys DEFERRABLE INITIALLY DEFERRED. Inside the test's transaction, a row may therefore
        # point at a project that does not exist yet, as during a fixture load that inserts a task before its project.
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO tenancy_task (project_id, title, archived, tenant_id, project_name) '
                'VALUES (%s, %s, false, %s, %s) RETURNING tenant_id, project_name',
                [999999, 'Early', tenant.pk, 'From the fixture'],
            )
            self.assertEqual(cursor.fetchone(), (tenant.pk, 'From the fixture'))

        # The load then inserts the project. The deferred constraint is checked against it at the end.
        Project.objects.create(pk=999999, tenant=tenant, name='Later')

    def test_a_leaf_that_the_application_inserts_before_its_ancestor_gets_the_values_of_the_ancestor(self):
        """
        Case: In one transaction, the application creates a task for the project with the primary key 999999, which
              does not exist yet, and gives the task the tenant Acme. Then it creates that project, with the tenant
              Other and the name Later.
        Expected: The task has the tenant Other and the project name Later.
        """
        acme = Tenant.objects.create(name='Acme')
        other = Tenant.objects.create(name='Other')

        task = Task.objects.create(project_id=999999, tenant=acme, title='Early')
        Project.objects.create(pk=999999, tenant=other, name='Later')

        task.refresh_from_db()
        self.assertEqual((task.tenant_id, task.project_name), (other.pk, 'Later'))


class ListingIndexTestCase(TestCase):
    """
    Postgres answers a tenant's ordered task listing from tenancy_task_listing, an index that is only possible because
    of the copies.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name='Acme')
        for number in range(3):
            project = Project.objects.create(tenant=self.tenant, name=f'Project {number}')
            Task.objects.bulk_create(Task(project=project, title=f'Task {number}-{task}') for task in range(10))
        with connection.cursor() as cursor:
            # ANALYZE collects statistics, so the planner knows how selective each index is and does not guess on a
            # fresh table.
            cursor.execute('ANALYZE tenancy_task')
            cursor.execute('SET LOCAL enable_seqscan = off')
            cursor.execute('SET LOCAL enable_sort = off')

    def test_the_ordered_listing_of_a_tenant_reads_the_index(self):
        """
        Case: The query plan for the active tasks of Acme, with sequential scans and sorts switched off.
        Expected: The plan uses the index tenancy_task_listing.
        """
        plan = Task.objects.filter(tenant=self.tenant, archived=False).values('id').explain()

        self.assertIn('tenancy_task_listing', plan)
