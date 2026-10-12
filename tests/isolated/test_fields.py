from unittest import skipUnless

from django.contrib.postgres.fields import ArrayField
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, models
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.operations import AlterField
from django.db.migrations.state import ProjectState
from django.db.models.functions import Length
from django.test import SimpleTestCase
from django.test.utils import isolate_apps

from postgres_denormalize.fields import (
    INFERRED_TARGET,
    DenormalizedField,
    DenormalizedForeignKey,
    get_denormalized_fields,
    infer_on_delete,
)


@isolate_apps('isolated')
class UnresolvedTargetTestCase(SimpleTestCase):
    """
    Until every model along the lookup path is loaded, the target is unknown. A copy of the field that is made during
    that time infers its own target. A history package makes such a copy while the model class is created.
    """

    def test_a_copy_taken_before_resolution_infers_its_own_target(self):
        """
        Case: Leaf is declared with a DenormalizedForeignKey on parent__owner before Parent and Owner exist. The field
              is deconstructed and rebuilt. Then Owner and Parent are declared.
        Expected: Before Owner and Parent exist, the target is unknown, the deconstructed field has no ``to``, and the
                  rebuilt field infers its target from the path. After they exist, the target is Owner and the field
                  passes its checks.
        """

        class Leaf(models.Model):
            parent = models.ForeignKey('Parent', on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE)

        field = Leaf._meta.get_field('owner')
        self.assertFalse(field.target_is_known)
        _name, _path, args, kwargs = field.deconstruct()
        self.assertNotIn('to', kwargs)
        self.assertTrue(DenormalizedForeignKey(*args, **kwargs).target_from_path)

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        self.assertTrue(field.target_is_known)
        self.assertIs(field.related_model, Owner)
        self.assertEqual(field.check(), [])

    def test_a_path_through_a_model_that_is_never_installed_reports_it(self):
        """
        Case: The lookup path parent__owner__name crosses Parent.owner, which points at a model that is never
              installed.
        Expected: The target is unknown, and the field reports error E001, which says that the model is not installed.
        """

        class Leaf(models.Model):
            parent = models.ForeignKey('Parent', on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__owner__name', on_delete=models.CASCADE)

        class Parent(models.Model):
            owner = models.ForeignKey('NotInstalled', on_delete=models.CASCADE)

        field = Leaf._meta.get_field('owner')
        errors = field.check()

        self.assertEqual(field.remote_field.model, INFERRED_TARGET)
        self.assertEqual([error.id for error in errors], ['postgres_denormalize.E001'])
        self.assertIn('not installed', errors[0].msg)

    def test_a_segment_that_names_no_field_leaves_the_target_unknown(self):
        """
        Case: The lookup path parent__missing names a field that Parent does not have.
        Expected: The target is unknown, and the field reports error E001.
        """

        class Leaf(models.Model):
            parent = models.ForeignKey('Parent', on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__missing', on_delete=models.CASCADE)

        class Parent(models.Model):
            pass

        field = Leaf._meta.get_field('owner')

        self.assertFalse(field.target_is_known)
        self.assertEqual([error.id for error in field.check()], ['postgres_denormalize.E001'])

    def test_a_plain_column_along_the_path_leaves_the_target_unknown(self):
        """
        Case: The lookup path parent__name__owner crosses Parent.name, which is a plain column.
        Expected: The target is unknown, and the field reports error E001.
        """

        class Leaf(models.Model):
            parent = models.ForeignKey('Parent', on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__name__owner', on_delete=models.CASCADE)

        class Parent(models.Model):
            name = models.CharField(max_length=10)

        field = Leaf._meta.get_field('owner')

        self.assertFalse(field.target_is_known)
        self.assertEqual([error.id for error in field.check()], ['postgres_denormalize.E001'])

    def test_related_names_are_formatted_on_the_leaf(self):
        """
        Case: The field declares related_name='%(app_label)s_%(class)s_set' and related_query_name='%(class)s_q'.
        Expected: The names are formatted with the leaf's app label and class, and Owner has the reverse accessor
                  isolated_leaf_set.
        """

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey(
                'parent__owner',
                on_delete=models.CASCADE,
                related_name='%(app_label)s_%(class)s_set',
                related_query_name='%(class)s_q',
            )

        field = Leaf._meta.get_field('owner')

        self.assertEqual(field.remote_field.related_name, 'isolated_leaf_set')
        self.assertEqual(field.remote_field.related_query_name, 'leaf_q')
        self.assertTrue(hasattr(Owner, 'isolated_leaf_set'))

    def test_the_default_related_name_is_formatted_too(self):
        """
        Case: The leaf's Meta.default_related_name is '%(model_name)s_items', and the field declares no related_name.
        Expected: The field's related name is leaf_items.
        """

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE)

            class Meta:
                default_related_name = '%(model_name)s_items'

        self.assertEqual(Leaf._meta.get_field('owner').remote_field.related_name, 'leaf_items')

    def test_a_field_on_an_abstract_model_infers_on_each_concrete_child(self):
        """
        Case: An abstract model declares the field, and a concrete model inherits from it.
        Expected: The field on the concrete model has the target Owner.
        """

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Base(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE, related_name='+')
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE, related_name='+')

            class Meta:
                abstract = True

        class Leaf(Base):
            pass

        self.assertIs(Leaf._meta.get_field('owner').related_model, Owner)


@isolate_apps('isolated')
class RefusedPathTestCase(SimpleTestCase):
    """
    A lookup path never crosses or ends at a denormalized field, and never crosses a field that a model inherits from a
    concrete parent. The check names the path to use instead.
    """

    def test_a_path_through_a_denormalized_foreign_key_is_refused(self):
        """
        Case: Child copies parent__owner into owner. Leaf copies child__owner, which crosses that copy.
        Expected: Leaf.owner reports error E006, whose hint names the path child__parent__owner.
        """

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Child(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE, related_name='+')

        class Leaf(models.Model):
            child = models.ForeignKey(Child, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('child__owner', on_delete=models.CASCADE, related_name='+')

        errors = Leaf._meta.get_field('owner').check()

        self.assertEqual([error.id for error in errors], ['postgres_denormalize.E006'])
        self.assertIn("'child__parent__owner'", errors[0].hint)

    def test_a_path_through_a_copy_whose_target_is_not_known_yet_is_refused(self):
        """
        Case: Leaf copies child__owner and is declared first. Child copies parent__owner and is declared before Parent
              and Owner, so the target of Child.owner is not known while Leaf's path is walked.
        Expected: Once every model is declared, the target of Leaf.owner is still unknown, and Leaf.owner reports error
                  E006 instead of raising.
        """

        class Leaf(models.Model):
            child = models.ForeignKey('Child', on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('child__owner', on_delete=models.CASCADE, related_name='+')

        class Child(models.Model):
            parent = models.ForeignKey('Parent', on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE, related_name='+')

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        field = Leaf._meta.get_field('owner')

        self.assertFalse(field.target_is_known)
        self.assertEqual([error.id for error in field.check()], ['postgres_denormalize.E006'])

    def test_a_path_that_ends_at_a_denormalized_field_is_refused(self):
        """
        Case: Child copies parent__name into parent_name. Leaf copies child__parent_name, which ends at that copy.
        Expected: Leaf.name reports error E006, whose hint names the path child__parent__name.
        """

        class Parent(models.Model):
            name = models.CharField(max_length=40)

        class Child(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            parent_name = DenormalizedField('parent__name')

        class Leaf(models.Model):
            child = models.ForeignKey(Child, on_delete=models.CASCADE)
            name = DenormalizedField('child__parent_name')

        errors = Leaf._meta.get_field('name').check()

        self.assertEqual([error.id for error in errors], ['postgres_denormalize.E006'])
        self.assertIn("'child__parent__name'", errors[0].hint)

    def test_a_hop_inherited_from_a_concrete_parent_is_refused_and_the_parent_link_is_suggested(self):
        """
        Case: Base has the foreign key owner, and Child inherits from Base in its own table. Leaf copies child__owner,
              and also child__base_ptr__owner, which walks to Base through the parent link.
        Expected: The copy of child__owner reports error E007, whose hint names the path child__base_ptr__owner. The
                  copy of child__base_ptr__owner passes its checks and has the target Owner.
        """

        class Owner(models.Model):
            pass

        class Base(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Child(Base):
            pass

        class Leaf(models.Model):
            child = models.ForeignKey(Child, on_delete=models.CASCADE)
            inherited = DenormalizedForeignKey('child__owner', on_delete=models.CASCADE, related_name='+')
            linked = DenormalizedForeignKey('child__base_ptr__owner', on_delete=models.CASCADE, related_name='+')

        errors = Leaf._meta.get_field('inherited').check()
        linked = Leaf._meta.get_field('linked')

        self.assertEqual([error.id for error in errors], ['postgres_denormalize.E007'])
        self.assertIn("'child__base_ptr__owner'", errors[0].hint)
        self.assertEqual(linked.check(), [])
        self.assertIs(linked.related_model, Owner)

    def test_a_one_to_one_hop_is_crossed(self):
        """
        Case: Profile has a one-to-one field user, and User has the foreign key owner. Profile copies user__owner.
        Expected: The copy passes its checks and has the target Owner.
        """

        class Owner(models.Model):
            pass

        class User(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Profile(models.Model):
            user = models.OneToOneField(User, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('user__owner', on_delete=models.CASCADE, related_name='+')

        field = Profile._meta.get_field('owner')

        self.assertEqual(field.check(), [])
        self.assertIs(field.related_model, Owner)


@isolate_apps('isolated')
class InheritedDenormalizedFieldTestCase(SimpleTestCase):
    def test_only_the_model_that_has_the_column_has_the_denormalized_field(self):
        """
        Case: Base copies parent__owner. Child inherits from Base in its own table, and ChildProxy is a proxy of Child.
        Expected: get_denormalized_fields returns the copy for Base, and no fields for Child and ChildProxy, because
                  the column is in the table of Base.
        """

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Base(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE, related_name='+')
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE, related_name='+')

        class Child(Base):
            pass

        class ChildProxy(Child):
            class Meta:
                proxy = True

        self.assertEqual(get_denormalized_fields(Base), [Base._meta.get_field('owner')])
        self.assertEqual(get_denormalized_fields(Child), [])
        self.assertEqual(get_denormalized_fields(ChildProxy), [])


@isolate_apps('isolated', attr_name='apps')
class ColumnTypeInferenceTestCase(SimpleTestCase):
    def declare_leaf(self, *paths, nullable_parent=False):
        """
        Declare a leaf two hops below a grandparent, with one copy per lookup path, named copy0, copy1 and so on.

        The grandparent has a CharField name, a nullable CharField label with a unique constraint, an index, a default
        and help text, and a generated column total.
        """

        class Grandparent(models.Model):
            name = models.CharField(max_length=40)
            label = models.CharField(
                'label', max_length=20, null=True, blank=True, unique=True, db_index=True, default='x', help_text='y'
            )
            total = models.GeneratedField(
                expression=Length('name'), output_field=models.IntegerField(), db_persist=True
            )

        class Parent(models.Model):
            grandparent = models.ForeignKey(Grandparent, on_delete=models.CASCADE, null=nullable_parent)

        copies = {f'copy{index}': DenormalizedField(path) for index, path in enumerate(paths)}
        return type(
            'Leaf',
            (models.Model,),
            {'__module__': __name__, 'parent': models.ForeignKey(Parent, on_delete=models.CASCADE), **copies},
        )

    def test_only_the_type_of_the_source_column_is_copied(self):
        """
        Case: A copy of the grandparent's label.
        Expected: The base field is a nullable CharField with max_length=20, without the label's other attributes.
        """
        field = self.declare_leaf('parent__grandparent__label')._meta.get_field('copy0')

        self.assertEqual(
            field.base_field.deconstruct()[1:], ('django.db.models.CharField', [], {'max_length': 20, 'null': True})
        )
        self.assertTrue(field.null)

    def test_a_nullable_hop_makes_the_copy_nullable(self):
        """
        Case: A copy of the grandparent's name, which is not nullable, through a nullable foreign key from parent to
              grandparent.
        Expected: The copy is nullable.
        """
        field = self.declare_leaf('parent__grandparent__name', nullable_parent=True)._meta.get_field('copy0')

        self.assertTrue(field.null)

    def test_the_check_refuses_a_path_ending_at_a_key_or_a_generated_column(self):
        """
        Case: Copies whose lookup paths end at a foreign key, at a primary key, at a generated column, at a column of
              the leaf itself, and at a field that does not exist.
        Expected: Each copy reports one error that says why, infers no base field, writes no base field into its
                  deconstruction, and raises an error that it cannot infer its column type when asked for its
                  database type.
        """
        expected = (
            ('parent__grandparent', 'DenormalizedForeignKey'),
            ('parent__grandparent__id', 'primary key'),
            ('parent__grandparent__total', 'generated column'),
            ('parent', 'same row'),
            ('parent__missing', 'does not resolve'),
        )
        leaf = self.declare_leaf(*(path for path, _named in expected))

        for index, (path, named) in enumerate(expected):
            with self.subTest(path=path):
                field = leaf._meta.get_field(f'copy{index}')
                errors = [error for error in field.check() if error.id.startswith('postgres_denormalize.')]

                self.assertEqual(len(errors), 1)
                self.assertIn(named, errors[0].msg)
                self.assertFalse(field.resolve_base_field())
                self.assertNotIn('base_field', field.deconstruct()[3])
                with self.assertRaisesMessage(Exception, 'cannot infer its column type'):
                    field.db_type(connection)

    def test_an_unbound_field_cannot_infer_its_type(self):
        """
        Case: A DenormalizedField that is not added to any model.
        Expected: It cannot infer its base field, and it is not nullable.
        """
        field = DenormalizedField('parent__name')

        self.assertFalse(field.resolve_base_field())
        self.assertFalse(field.null)

    def test_a_base_field_passed_in_takes_precedence_over_the_source_column(self):
        """
        Case: A copy of a CharField with max_length=40 is declared with base_field=CharField(max_length=10).
        Expected: The copy has the database type varchar(10).
        """

        class Grandparent(models.Model):
            name = models.CharField(max_length=40)

        class Leaf(models.Model):
            grandparent = models.ForeignKey(Grandparent, on_delete=models.CASCADE)
            copy = DenormalizedField('grandparent__name', base_field=models.CharField(max_length=10))

        self.assertEqual(Leaf._meta.get_field('copy').db_type(connection), 'varchar(10)')

    def test_widening_the_source_column_makes_makemigrations_alter_the_copy(self):
        """
        Case: In the migration state, the source column and the copy have max_length=40. In the models, the source
              column has max_length=80.
        Expected: The autodetector writes an AlterField for the copy.
        """

        class Grandparent(models.Model):
            name = models.CharField(max_length=80)

        class Leaf(models.Model):
            grandparent = models.ForeignKey(Grandparent, on_delete=models.CASCADE)
            copy = DenormalizedField('grandparent__name')

        to_state = ProjectState.from_apps(self.apps)
        from_state = to_state.clone()
        from_state.models['isolated', 'grandparent'].fields['name'] = models.CharField(max_length=40)
        from_state.models['isolated', 'leaf'].fields['copy'] = DenormalizedField(
            'grandparent__name', base_field=models.CharField(max_length=40)
        )

        changes = MigrationAutodetector(from_state, to_state)._detect_changes()

        altered = [
            (operation.model_name, operation.name)
            for migration in changes['isolated']
            for operation in migration.operations
            if isinstance(operation, AlterField)
        ]
        self.assertIn(('leaf', 'copy'), altered)

    def test_copies_of_array_and_json_columns_are_serialized_so_that_they_can_be_read_back(self):
        """
        Case: Leaf copies an ArrayField of strings and a JSONField of its grandparent. A leaf row has the tags a and b
              and the settings {'a': 1}. Each copy writes its value the way the serializers do, with value_to_string,
              and reads it back with to_python, as loaddata does.
        Expected: The array copy writes the JSON text ["a", "b"], and the JSON copy writes the dict itself. Both read
                  back the values of the row.
        """

        class Grandparent(models.Model):
            tags = ArrayField(models.CharField(max_length=10))
            settings = models.JSONField()

        class Leaf(models.Model):
            grandparent = models.ForeignKey(Grandparent, on_delete=models.CASCADE)
            tags = DenormalizedField('grandparent__tags')
            settings = DenormalizedField('grandparent__settings')

        leaf = Leaf(tags=['a', 'b'], settings={'a': 1})
        tags = Leaf._meta.get_field('tags')
        settings = Leaf._meta.get_field('settings')

        self.assertEqual(tags.value_to_string(leaf), '["a", "b"]')
        self.assertEqual(tags.to_python(tags.value_to_string(leaf)), ['a', 'b'])
        self.assertEqual(settings.value_to_string(leaf), {'a': 1})
        self.assertEqual(settings.to_python(settings.value_to_string(leaf)), {'a': 1})


@isolate_apps('isolated')
class ForeignKeyChecksTestCase(SimpleTestCase):
    def test_a_copy_through_a_nullable_hop_must_be_nullable(self):
        """
        Case: Parent.owner is nullable. Leaf copies parent__owner three times: declared with null=False, declared with
              null=True, and declared without null.
        Expected: The copy with null=False reports error E004. The other two copies are nullable and pass their checks.
        """

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE, null=True)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            required = DenormalizedForeignKey('parent__owner', null=False, related_name='+')
            optional = DenormalizedForeignKey('parent__owner', null=True, related_name='+')
            inferred = DenormalizedForeignKey('parent__owner', related_name='+')

        self.assertEqual(
            [error.id for error in Leaf._meta.get_field('required').check()], ['postgres_denormalize.E004']
        )
        for name in ('optional', 'inferred'):
            with self.subTest(name=name):
                self.assertTrue(Leaf._meta.get_field(name).null)
                self.assertEqual(Leaf._meta.get_field(name).check(), [])

    def test_a_nullable_first_hop_is_refused(self):
        """
        Case: Leaf.parent, the first hop of the lookup path, is nullable.
        Expected: Error E002.
        """

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE, null=True)
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE, null=True)

        self.assertEqual([error.id for error in Leaf._meta.get_field('owner').check()], ['postgres_denormalize.E002'])

    def test_an_explicit_target_that_differs_from_the_path_is_refused(self):
        """
        Case: The lookup path parent__owner ends at Owner, but the field is declared with to=Other.
        Expected: Error E003.
        """

        class Owner(models.Model):
            pass

        class Other(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE, to=Other)

        errors = Leaf._meta.get_field('owner').check()

        self.assertEqual([error.id for error in errors], ['postgres_denormalize.E003'])
        self.assertIn('isolated.Other', errors[0].msg)
        self.assertIn('isolated.Owner', errors[0].msg)

    def test_an_explicit_target_column_that_differs_from_the_path_is_refused(self):
        """
        Case: Parent.owner points at Owner by its unique code. Leaf copies parent__owner with to_field='id'.
        Expected: Error E003, which names the columns isolated.Owner.id and isolated.Owner.code.
        """

        class Owner(models.Model):
            code = models.CharField(max_length=10, unique=True)

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE, to_field='code')

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE, to_field='id', related_name='+')

        errors = Leaf._meta.get_field('owner').check()

        self.assertEqual([error.id for error in errors], ['postgres_denormalize.E003'])
        self.assertIn('isolated.Owner.id', errors[0].msg)
        self.assertIn('isolated.Owner.code', errors[0].msg)

    def test_a_target_that_is_not_known_while_the_path_resolves_is_reported(self):
        """
        Case: The lookup path parent__owner resolves, but the field's target is reset to the reference that stands for
              a target that is not known yet.
        Expected: Error E003, which says that the target is not known and names isolated.Owner.
        """

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE, related_name='+')

        field = Leaf._meta.get_field('owner')
        field.remote_field.model = INFERRED_TARGET

        errors = field.check()

        self.assertEqual([error.id for error in errors], ['postgres_denormalize.E003'])
        self.assertIn('not known', errors[0].msg)
        self.assertIn('isolated.Owner', errors[0].msg)


def custom_rule(collector, field, sub_objs, using):
    pass


@isolate_apps('isolated', attr_name='apps')
class OnDeleteInferenceTestCase(SimpleTestCase):
    """
    A DenormalizedForeignKey without ``on_delete`` or ``null`` infers both from the hops of its lookup path.
    """

    def declare_copy(self, suffix, leaf_rule=models.CASCADE, parent_rule=models.CASCADE, nullable=True, **kwargs):
        """
        Declare Owner, Parent and Leaf, each with ``suffix`` appended to its name, and return the field Leaf.owner.

        Leaf.parent has the rule ``leaf_rule``. Parent.owner has the rule ``parent_rule``, and is nullable when
        ``nullable`` is true. Leaf.owner copies parent__owner, declared with ``kwargs``.
        """
        owner = type(f'Owner{suffix}', (models.Model,), {'__module__': __name__})
        parent = type(
            f'Parent{suffix}',
            (models.Model,),
            {'__module__': __name__, 'owner': models.ForeignKey(owner, on_delete=parent_rule, null=nullable)},
        )
        leaf = type(
            f'Leaf{suffix}',
            (models.Model,),
            {
                '__module__': __name__,
                'parent': models.ForeignKey(parent, on_delete=leaf_rule),
                'owner': DenormalizedForeignKey('parent__owner', related_name='+', **kwargs),
            },
        )
        return leaf._meta.get_field('owner')

    def assertInfers(self, cases):
        for index, (leaf_rule, parent_rule, expected) in enumerate(cases):
            with self.subTest(leaf_rule=leaf_rule, parent_rule=parent_rule):
                field = self.declare_copy(index, leaf_rule, parent_rule)

                self.assertIs(field.remote_field.on_delete, expected)
                self.assertEqual(field.check(), [])

    def test_the_first_hop_that_does_not_cascade_decides_the_rule(self):
        """
        Case: Copies along paths of two hops, with each combination of rules below. Parent.owner is nullable.
        Expected: A path that only cascades gives CASCADE. Otherwise the hop next to the owner decides, unless it
                  cascades: SET_NULL, PROTECT and RESTRICT are copied, and SET_DEFAULT, SET and DO_NOTHING give
                  DO_NOTHING. Each copy passes its checks.
        """
        self.assertInfers(
            [
                (models.CASCADE, models.CASCADE, models.CASCADE),
                (models.CASCADE, models.SET_NULL, models.SET_NULL),
                (models.CASCADE, models.PROTECT, models.PROTECT),
                (models.CASCADE, models.RESTRICT, models.RESTRICT),
                (models.CASCADE, models.SET_DEFAULT, models.DO_NOTHING),
                (models.CASCADE, models.SET(None), models.DO_NOTHING),
                (models.CASCADE, models.DO_NOTHING, models.DO_NOTHING),
                (models.PROTECT, models.CASCADE, models.PROTECT),
                (models.SET_DEFAULT, models.CASCADE, models.DO_NOTHING),
                (models.PROTECT, models.SET_NULL, models.SET_NULL),
            ]
        )

    @skipUnless(hasattr(models, 'DB_CASCADE'), 'The database rules exist from Django 6.1 on.')
    def test_the_database_rules_infer_database_rules(self):
        """
        Case: Copies along paths of two hops with the database rules DB_CASCADE, DB_SET_NULL and DB_SET_DEFAULT.
        Expected: DB_CASCADE on both hops gives DB_CASCADE, DB_SET_NULL gives DB_SET_NULL, and DB_SET_DEFAULT gives
                  DO_NOTHING.
        """
        self.assertInfers(
            [
                (models.DB_CASCADE, models.DB_CASCADE, models.DB_CASCADE),
                (models.DB_CASCADE, models.DB_SET_NULL, models.DB_SET_NULL),
                (models.DB_CASCADE, models.DB_SET_DEFAULT, models.DO_NOTHING),
            ]
        )

    def test_null_follows_the_hops_after_the_first(self):
        """
        Case: One copy along a path whose second hop is nullable, and one along a path without a nullable hop.
        Expected: The first copy is nullable and writes null=True into its deconstruction. The second is not nullable
                  and writes null=False.
        """
        nullable = self.declare_copy('Nullable', nullable=True)
        required = self.declare_copy('Required', nullable=False)

        self.assertTrue(nullable.null)
        self.assertIs(nullable.deconstruct()[3]['null'], True)
        self.assertFalse(required.null)
        self.assertIs(required.deconstruct()[3]['null'], False)

    def test_a_field_in_the_migration_state_keeps_its_null_when_a_hop_becomes_nullable(self):
        """
        Case: The migration state has Leaf.owner along parent__owner, where Parent.owner is not nullable. A migration
              makes Parent.owner nullable, and the state is rendered again.
        Expected: Leaf.owner in the rendered state is still not nullable, because its deconstruction recorded
                  null=False. A later AlterField that makes the copy nullable therefore changes the column.
        """
        self.declare_copy('', nullable=False)
        state = ProjectState.from_apps(self.apps)
        AlterField(
            'parent', 'owner', models.ForeignKey('isolated.owner', on_delete=models.CASCADE, null=True)
        ).state_forwards('isolated', state)

        rendered = state.apps.get_model('isolated', 'leaf')._meta.get_field('owner')

        self.assertFalse(rendered.null)
        self.assertFalse(rendered.null_from_path)

    def test_an_explicit_target_still_infers_the_rule_and_null(self):
        """
        Case: A copy along a path whose second hop is SET_NULL, declared with to= the owner, without on_delete or null.
        Expected: The copy infers SET_NULL and null=True, and passes its checks.
        """
        owner = type('Owner', (models.Model,), {'__module__': __name__})
        parent = type(
            'Parent',
            (models.Model,),
            {'__module__': __name__, 'owner': models.ForeignKey(owner, on_delete=models.SET_NULL, null=True)},
        )
        leaf = type(
            'Leaf',
            (models.Model,),
            {
                '__module__': __name__,
                'parent': models.ForeignKey(parent, on_delete=models.CASCADE),
                'owner': DenormalizedForeignKey('parent__owner', to=owner, related_name='+'),
            },
        )
        field = leaf._meta.get_field('owner')

        self.assertIs(field.remote_field.on_delete, models.SET_NULL)
        self.assertTrue(field.null)
        self.assertEqual(field.check(), [])

    def test_a_copy_taken_before_resolution_infers_the_rule_itself(self):
        """
        Case: Leaf is declared with a DenormalizedForeignKey on parent__owner before Parent and Owner exist. The field
              is deconstructed and rebuilt. Then Owner, and Parent with a nullable SET_NULL owner, are declared.
        Expected: Before Owner and Parent exist, the deconstructed field has neither on_delete nor null, and the rebuilt
                  field infers both. After they exist, the field has SET_NULL and null=True, and its deconstruction
                  records both.
        """

        class Leaf(models.Model):
            parent = models.ForeignKey('Parent', on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__owner', related_name='+')

        field = Leaf._meta.get_field('owner')
        self.assertFalse(field.on_delete_is_known)
        _name, _path, args, kwargs = field.deconstruct()
        self.assertNotIn('on_delete', kwargs)
        self.assertNotIn('null', kwargs)
        rebuilt = DenormalizedForeignKey(*args, **kwargs)
        self.assertTrue(rebuilt.on_delete_from_path)
        self.assertTrue(rebuilt.null_from_path)

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.SET_NULL, null=True)

        kwargs = field.deconstruct()[3]
        self.assertIs(kwargs['on_delete'], models.SET_NULL)
        self.assertIs(kwargs['null'], True)

    def test_a_custom_rule_along_the_path_is_reported(self):
        """
        Case: Parent.owner has a custom on_delete rule. One copy is declared without on_delete, one with CASCADE.
        Expected: The copy without on_delete reports error E008, which names the hop and the rule, writes no on_delete
                  into its deconstruction, and refuses a delete. The copy with CASCADE passes its checks.
        """
        inferred = self.declare_copy('Inferred', parent_rule=custom_rule)
        declared = self.declare_copy('Declared', parent_rule=custom_rule, on_delete=models.CASCADE)

        errors = inferred.check()

        self.assertEqual([error.id for error in errors], ['postgres_denormalize.E008'])
        self.assertIn('isolated.ParentInferred.owner', errors[0].msg)
        self.assertIn('custom_rule', errors[0].msg)
        self.assertNotIn('on_delete', inferred.deconstruct()[3])
        with self.assertRaisesMessage(ImproperlyConfigured, 'cannot infer its on_delete rule'):
            inferred.remote_field.on_delete(None, inferred, [], 'default')
        self.assertEqual(declared.check(), [])

    def test_a_declared_rule_that_differs_from_the_path_is_a_warning(self):
        """
        Case: Parent.owner is SET_NULL. One copy is declared with on_delete=CASCADE, one with SET(None), and one with
              SET_NULL.
        Expected: The copies with CASCADE and SET(None) report warning W009, which names the declared rule, SET_NULL and
                  the hop. The copy with SET_NULL passes its checks.
        """
        for index, (rule, name) in enumerate([(models.CASCADE, 'CASCADE'), (models.SET(None), 'SET(None)')]):
            with self.subTest(rule=name):
                errors = self.declare_copy(index, parent_rule=models.SET_NULL, on_delete=rule, null=True).check()

                self.assertEqual([error.id for error in errors], ['postgres_denormalize.W009'])
                self.assertIsInstance(errors[0], checks.Warning)
                self.assertIn(f'on_delete={name}', errors[0].msg)
                self.assertIn('implies SET_NULL', errors[0].msg)
                self.assertIn(f'isolated.Parent{index}.owner', errors[0].msg)

        matching = self.declare_copy('Matching', parent_rule=models.SET_NULL, on_delete=models.SET_NULL, null=True)
        self.assertEqual(matching.check(), [])

    def test_declaring_the_inferred_values_writes_no_migration(self):
        """
        Case: In the migration state, Leaf.owner is declared with on_delete=SET_NULL and null=True. In the models, it is
              declared without both, along a path whose second hop is a nullable SET_NULL.
        Expected: The autodetector writes no operations.
        """
        self.declare_copy('', parent_rule=models.SET_NULL)
        to_state = ProjectState.from_apps(self.apps)
        from_state = to_state.clone()
        from_state.models['isolated', 'leaf'].fields['owner'] = DenormalizedForeignKey(
            'parent__owner', on_delete=models.SET_NULL, null=True, to='isolated.owner', related_name='+'
        )

        self.assertEqual(MigrationAutodetector(from_state, to_state)._detect_changes(), {})

    def test_infer_on_delete_names_the_hop_that_decides(self):
        """
        Case: infer_on_delete on a path whose first hop is PROTECT and whose second hop cascades.
        Expected: PROTECT, decided by the first hop.
        """
        field = self.declare_copy('', leaf_rule=models.PROTECT)
        chain = field.resolve_chain()

        self.assertEqual(infer_on_delete(chain), (models.PROTECT, chain[0]))
