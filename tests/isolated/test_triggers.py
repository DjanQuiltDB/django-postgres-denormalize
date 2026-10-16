from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.db import connection, models
from django.test import SimpleTestCase
from django.test.utils import isolate_apps

from postgres_denormalize.fields import DenormalizedField, DenormalizedForeignKey
from postgres_denormalize.triggers import DenormalizeCascadeTrigger, DenormalizeSetTrigger, register_triggers_for


@isolate_apps('isolated')
class UnresolvablePathTestCase(SimpleTestCase):
    def declare_leaf(self):
        """
        Declare a leaf whose only denormalized field has the misspelt lookup path parnet__owner.
        """

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parnet__owner', on_delete=models.CASCADE, to=Owner)

        return Leaf

    def test_the_set_trigger_compiles_without_the_unresolvable_field(self):
        """
        Case: The set trigger is compiled for the leaf.
        Expected: The function only returns NEW, and declares no variables.
        """
        leaf = self.declare_leaf()

        self.assertEqual(DenormalizeSetTrigger().get_func(leaf), 'RETURN NEW;')
        self.assertEqual(DenormalizeSetTrigger().get_declare(leaf), [])

    def test_registration_skips_a_leaf_whose_only_field_does_not_resolve(self):
        """
        Case: The triggers are registered for the leaf.
        Expected: The leaf gets no triggers.
        """
        leaf = self.declare_leaf()

        register_triggers_for(leaf)

        self.assertEqual(getattr(leaf._meta, 'triggers', []), [])


@isolate_apps('isolated')
class QuotedNamesTestCase(SimpleTestCase):
    def test_mixed_case_table_and_column_names_are_quoted(self):
        """
        Case: Project has db_table='Legacy_Project' and its foreign key tenant has db_column='TenantID'. Task copies
              project__tenant. The set trigger of Task and its cascade trigger for Project are compiled.
        Expected: Both functions name the table as "Legacy_Project" and the column as "TenantID", so Postgres does not
                  fold them to lowercase.
        """

        class Tenant(models.Model):
            pass

        class Project(models.Model):
            tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column='TenantID')

            class Meta:
                db_table = 'Legacy_Project'

        class Task(models.Model):
            project = models.ForeignKey(Project, on_delete=models.CASCADE)
            tenant = DenormalizedForeignKey('project__tenant', on_delete=models.CASCADE, related_name='+')

        set_func = DenormalizeSetTrigger().get_func(Task)
        cascade = DenormalizeCascadeTrigger(leaf_label='isolated.Task')

        self.assertIn('FROM "Legacy_Project" hop0', set_func)
        self.assertIn('hop0."TenantID" AS value0', set_func)
        self.assertIn('SET "tenant_id" = NEW."TenantID"', cascade.get_func(Project))
        self.assertIn('OLD."TenantID" IS DISTINCT FROM NEW."TenantID"', cascade.get_condition(Project).resolve(Project))

    def test_names_are_quoted_without_the_default_database(self):
        """
        Case: The default database cannot quote names, as in a project whose default database is Django's dummy
              backend and whose Postgres database has another alias. The set trigger of a leaf is compiled.
        Expected: The function is compiled, and names the table as "isolated_parent".
        """

        class Parent(models.Model):
            name = models.CharField(max_length=40)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            parent_name = DenormalizedField('parent__name')

        with mock.patch.object(connection.ops, 'quote_name', side_effect=ImproperlyConfigured('dummy backend')):
            func = DenormalizeSetTrigger().get_func(Leaf)

        self.assertIn('FROM "isolated_parent" hop0', func)


@isolate_apps('isolated')
class OneToOneHopTestCase(SimpleTestCase):
    def test_a_one_to_one_hop_is_joined_like_a_foreign_key(self):
        """
        Case: Profile has a one-to-one field user, and User has the foreign key owner. Leaf has a foreign key to
              Profile and copies profile__user__owner. The set trigger of Leaf is compiled.
        Expected: The users table is joined on the column of the one-to-one field.
        """

        class Owner(models.Model):
            pass

        class User(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Profile(models.Model):
            user = models.OneToOneField(User, on_delete=models.CASCADE)

        class Leaf(models.Model):
            profile = models.ForeignKey(Profile, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('profile__user__owner', on_delete=models.CASCADE, related_name='+')

        func = DenormalizeSetTrigger().get_func(Leaf)

        self.assertIn('LEFT JOIN "isolated_user" hop1 ON hop1."id" = hop0."user_id"', func)


@isolate_apps('isolated')
class ToFieldTestCase(SimpleTestCase):
    """
    setUp declares Owner with the unique column code, Parent with a foreign key to Owner by code, Middle with a plain
    foreign key to Parent, and two leaves. ByCode has a foreign key to Parent by code, and copies the parent's owner.
    Plain has a foreign key to Middle by primary key, and copies the name of the owner of the middle's parent.
    """

    def setUp(self):
        class Owner(models.Model):
            code = models.CharField(max_length=10, unique=True)
            name = models.CharField(max_length=40)

        class Parent(models.Model):
            code = models.CharField(max_length=10, unique=True)
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE, to_field='code')

        class Middle(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)

        class ByCode(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE, to_field='code')
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE, related_name='+')

        class Plain(models.Model):
            middle = models.ForeignKey(Middle, on_delete=models.CASCADE)
            owner_name = DenormalizedField('middle__parent__owner__name')

        self.owner, self.parent, self.by_code, self.plain = Owner, Parent, ByCode, Plain

    def test_a_first_hop_by_code_is_looked_up_by_code(self):
        """
        Case: The set trigger of ByCode is compiled. Its first hop stores the code of a parent.
        Expected: The lookup compares the parent's code, not its primary key, with the first hop.
        """
        self.assertIn('WHERE hop0."code" = NEW."parent_id";', DenormalizeSetTrigger().get_func(self.by_code))

    def test_a_later_hop_by_code_is_joined_by_code(self):
        """
        Case: The set trigger of Plain is compiled. The hop from Parent to Owner stores the code of an owner.
        Expected: The owners table is joined on its code, not on its primary key.
        """
        func = DenormalizeSetTrigger().get_func(self.plain)

        self.assertIn('LEFT JOIN "isolated_owner" hop2 ON hop2."code" = hop1."owner_id"', func)

    def test_the_cascade_reaches_the_leaves_by_code(self):
        """
        Case: The cascade trigger of ByCode is compiled for Parent, and the cascade trigger of Plain for Owner and for
              Parent.
        Expected: The leaves of ByCode are found by the parent's code. The leaves of Plain are found through the parents
                  whose owner is the owner's code, and a moved owner is read by its code.
        """
        by_code = DenormalizeCascadeTrigger(leaf_label='isolated.ByCode')
        plain = DenormalizeCascadeTrigger(leaf_label='isolated.Plain')

        self.assertIn('WHERE leaf."parent_id" = NEW."code"\n', by_code.get_func(self.parent))
        self.assertIn('AND hop1."owner_id" = NEW."code"\n', plain.get_func(self.owner))
        self.assertIn('WHERE upper0."code" = NEW."owner_id")', plain.get_func(self.parent))

    def test_the_copy_infers_the_target_column_of_the_last_hop(self):
        """
        Case: ByCode.owner copies parent__owner, whose last hop stores the code of an owner, and declares no to_field.
        Expected: The copy's target field is Owner.code, and its deconstruction has to_field='code'.
        """
        field = self.by_code._meta.get_field('owner')

        self.assertEqual(field.target_field, self.owner._meta.get_field('code'))
        self.assertEqual(field.deconstruct()[3]['to_field'], 'code')
        self.assertEqual(field.check(), [])


@isolate_apps('isolated')
class CopiesReachingOneModelByDifferentPathsTestCase(SimpleTestCase):
    """
    When the copies of a leaf reach the same model through different hops, they cannot share one statement, because the
    leaf rows below a changed row are different for each path.
    """

    def test_each_path_gets_its_own_statement_guarded_by_its_own_columns(self):
        """
        Case: Leaf copies Owner.name through owner, and Owner.code through middle__owner. The cascade trigger for Leaf
              is compiled for Owner.
        Expected: The condition tests both columns. The function has two UPDATE statements, each inside an IF that
                  tests the column of its own path.
        """

        class Owner(models.Model):
            name = models.CharField(max_length=40)
            code = models.CharField(max_length=10)

        class Middle(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Leaf(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)
            middle = models.ForeignKey(Middle, on_delete=models.CASCADE)
            owner_name = DenormalizedField('owner__name')
            middle_owner_code = DenormalizedField('middle__owner__code')

        trigger = DenormalizeCascadeTrigger(leaf_label='isolated.Leaf')
        func = trigger.get_func(Owner)

        self.assertTrue(
            trigger.get_condition(Owner)
            .resolve(Owner)
            .startswith('((OLD."name" IS DISTINCT FROM NEW."name" OR OLD."code" IS DISTINCT FROM NEW."code") AND NOT ')
        )
        self.assertEqual(func.count('UPDATE '), 2)
        self.assertIn('IF OLD."name" IS DISTINCT FROM NEW."name" THEN\n    UPDATE "isolated_leaf" leaf\n', func)
        self.assertIn('IF OLD."code" IS DISTINCT FROM NEW."code" THEN\n    UPDATE "isolated_leaf" leaf\n', func)
