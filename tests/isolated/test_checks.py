from django.contrib.auth.models import User
from django.db import models
from django.db.models import F
from django.test import SimpleTestCase, override_settings
from django.test.utils import isolate_apps

from postgres_denormalize.checks import (
    alternative_paths,
    check_ordering_uses_copies,
    check_redundant_copies,
    third_party_owner_warnings,
)
from postgres_denormalize.fields import DenormalizedField, DenormalizedForeignKey


def override_redundant_copy_check(level='warning', allowed=None):
    return override_settings(POSTGRES_DENORMALIZE={'REDUNDANT_COPY_CHECK': {'LEVEL': level, 'ALLOWED': allowed or {}}})


@isolate_apps('isolated', attr_name='apps')
class RedundantCopyCheckTestCase(SimpleTestCase):
    """
    setUp declares Owner, Parent with a foreign key to Owner, and Leaf with a foreign key to Parent and a plain foreign
    key to Owner. Leaf.owner can therefore also be reached through parent__owner.
    """

    def setUp(self):
        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE, related_name='+')

        self.leaf = Leaf

    def run_redundant_copy_check(self):
        return check_redundant_copies(app_configs=self.apps.get_app_configs())

    def test_an_undeclared_copy_is_a_warning_by_default(self):
        """
        Case: The redundant copy check runs with the level 'warning'.
        Expected: Warning W101, which names the path parent__owner and suggests a DenormalizedForeignKey for it.
        """
        with override_redundant_copy_check():
            messages = self.run_redundant_copy_check()

        self.assertEqual([message.id for message in messages], ['postgres_denormalize.W101'])
        self.assertIn('parent__owner', messages[0].msg)
        self.assertIn("DenormalizedForeignKey('parent__owner', ...)", messages[0].hint)

    def test_the_level_can_be_raised_or_turned_off(self):
        """
        Case: The redundant copy check runs with the level 'error', and then with the level 'off'.
        Expected: With 'error', error E101. With 'off', no messages.
        """
        with override_redundant_copy_check(level='error'):
            self.assertEqual([message.id for message in self.run_redundant_copy_check()], ['postgres_denormalize.E101'])
        with override_redundant_copy_check(level='off'):
            self.assertEqual(self.run_redundant_copy_check(), [])

    def test_an_allowed_copy_passes(self):
        """
        Case: ALLOWED lists isolated.Leaf.owner with a reason.
        Expected: No messages.
        """
        with override_redundant_copy_check(allowed={'isolated.Leaf.owner': 'Chosen independently.'}):
            self.assertEqual(self.run_redundant_copy_check(), [])

    def test_an_allowed_entry_needs_a_reason(self):
        """
        Case: ALLOWED lists isolated.Leaf.owner with a reason that is only a space.
        Expected: Error E103.
        """
        with override_redundant_copy_check(allowed={'isolated.Leaf.owner': ' '}):
            self.assertEqual([message.id for message in self.run_redundant_copy_check()], ['postgres_denormalize.E103'])

    def test_an_allowed_entry_that_no_longer_applies_is_stale(self):
        """
        Case: ALLOWED lists isolated.Leaf.owner and isolated.Leaf.parent, but Leaf cannot reach the target of parent in
              any other way.
        Expected: One error E102, which names isolated.Leaf.parent.
        """
        allowed = {'isolated.Leaf.owner': 'Chosen.', 'isolated.Leaf.parent': 'Stale.'}
        with override_redundant_copy_check(allowed=allowed):
            messages = self.run_redundant_copy_check()

        self.assertEqual([message.id for message in messages], ['postgres_denormalize.E102'])
        self.assertIn('isolated.Leaf.parent', messages[0].msg)


@isolate_apps('isolated', attr_name='apps')
class DeclaredCopyTestCase(SimpleTestCase):
    def test_a_declared_copy_passes(self):
        """
        Case: Leaf.owner is a DenormalizedForeignKey with the lookup path parent__owner, and the redundant copy check
              runs with the level 'error'.
        Expected: No messages.
        """

        class Owner(models.Model):
            pass

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE, related_name='+')

        with override_redundant_copy_check(level='error'):
            self.assertEqual(check_redundant_copies(app_configs=self.apps.get_app_configs()), [])


@isolate_apps('isolated', attr_name='apps')
class InheritedForeignKeyTestCase(SimpleTestCase):
    def test_a_foreign_key_that_a_child_inherits_is_not_a_copy(self):
        """
        Case: Base has the foreign key owner, and Child inherits from Base in its own table. The redundant copy check
              runs with the level 'error'.
        Expected: No messages. Child.owner is the column of Base, which Child also reaches through its parent link,
                  and not a copy of it.
        """

        class Owner(models.Model):
            pass

        class Base(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Child(Base):
            pass

        with override_redundant_copy_check(level='error'):
            self.assertEqual(check_redundant_copies(app_configs=self.apps.get_app_configs()), [])


@isolate_apps('isolated')
class AlternativePathsTestCase(SimpleTestCase):
    def test_the_walk_is_breadth_first_and_never_starts_with_the_field_itself(self):
        """
        Case: Leaf has foreign keys to Root, to Long and to Middle, in that order. Long points at Middle, and Middle
              points at Root. The alternative paths of Leaf.root are collected.
        Expected: Root is reached through middle__root, Middle through long__middle, and Long, which only one hop
                  reaches, is not in the result.
        """

        class Root(models.Model):
            pass

        class Middle(models.Model):
            root = models.ForeignKey(Root, on_delete=models.CASCADE)

        class Long(models.Model):
            middle = models.ForeignKey(Middle, on_delete=models.CASCADE)

        class Leaf(models.Model):
            root = models.ForeignKey(Root, on_delete=models.CASCADE, related_name='+')
            long = models.ForeignKey(Long, on_delete=models.CASCADE)
            middle = models.ForeignKey(Middle, on_delete=models.CASCADE, related_name='+')

        paths = alternative_paths(Leaf, Leaf._meta.get_field('root'), max_depth=4)

        # Long is declared first, so a depth-first walk would reach Root through long__middle__root. A breadth-first
        # walk finds the two-hop route through `middle` first. A model that is reached in one hop is not an alternative
        # route at all.
        self.assertEqual(paths[Root], 'middle__root')
        self.assertEqual(paths[Middle], 'long__middle')
        self.assertNotIn(Long, paths)

    def test_the_walk_crosses_only_hops_that_a_lookup_path_may_cross(self):
        """
        Case: Child copies parent__owner into owner, and Leaf has a foreign key to Child and a plain foreign key to
              Owner. Profile has a one-to-one field to Leaf. Grandchild inherits from Child in its own table. The
              alternative paths of Leaf.owner, of Profile and of Grandchild are collected.
        Expected: Leaf reaches Owner through child__parent__owner and not through the copy child__owner. Profile
                  reaches Child through the one-to-one field, as leaf__child. Grandchild reaches Owner through its
                  parent link, as child_ptr__parent__owner, and not through the foreign key parent that it inherits.
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
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE, related_name='+')

        class Profile(models.Model):
            leaf = models.OneToOneField(Leaf, on_delete=models.CASCADE)

        class Grandchild(Child):
            pass

        leaf_paths = alternative_paths(Leaf, Leaf._meta.get_field('owner'), max_depth=4)

        self.assertEqual(leaf_paths[Owner], 'child__parent__owner')
        self.assertEqual(alternative_paths(Profile, None, max_depth=4)[Child], 'leaf__child')
        self.assertEqual(alternative_paths(Grandchild, None, max_depth=4)[Owner], 'child_ptr__parent__owner')


@isolate_apps('isolated', attr_name='apps')
class OrderingThroughCopiedPathTestCase(SimpleTestCase):
    """
    setUp declares a Leaf with a DenormalizedForeignKey owner that copies parent__owner. Its Meta.ordering has the
    entries '-parent__owner__name', F('parent').asc() and 'owner'.
    """

    def setUp(self):
        class Owner(models.Model):
            name = models.CharField(max_length=10)

        class Parent(models.Model):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            owner = DenormalizedForeignKey('parent__owner', on_delete=models.CASCADE, related_name='+')

            class Meta:
                ordering = ['-parent__owner__name', F('parent').asc(), 'owner']

    def run_ordering_check(self):
        return check_ordering_uses_copies(app_configs=self.apps.get_app_configs())

    @override_settings(POSTGRES_DENORMALIZE={})
    def test_an_ordering_through_a_copied_path_is_a_warning_by_default(self):
        """
        Case: The ordering check runs with its default level.
        Expected: One warning W201, which names the entry '-parent__owner__name'. Its hint keeps the direction and the
                  segment after the copied path: order by '-owner__name'.
        """
        messages = self.run_ordering_check()

        self.assertEqual([message.id for message in messages], ['postgres_denormalize.W201'])
        self.assertIn('-parent__owner__name', messages[0].msg)
        self.assertEqual(messages[0].hint, 'Order by `-owner__name` instead.')

    @override_settings(POSTGRES_DENORMALIZE={'ORDERING_CHECK_LEVEL': 'error'})
    def test_the_level_can_be_raised(self):
        """
        Case: The ordering check runs with ORDERING_CHECK_LEVEL set to 'error'.
        Expected: Error E201.
        """
        self.assertEqual([message.id for message in self.run_ordering_check()], ['postgres_denormalize.E201'])

    @override_settings(POSTGRES_DENORMALIZE={'ORDERING_CHECK_LEVEL': 'off'})
    def test_the_check_can_be_turned_off(self):
        """
        Case: The ordering check runs with ORDERING_CHECK_LEVEL set to 'off'.
        Expected: No messages.
        """
        self.assertEqual(self.run_ordering_check(), [])


@isolate_apps('isolated', attr_name='apps')
class OrderingThroughCopiedColumnNamedIdTestCase(SimpleTestCase):
    @override_settings(POSTGRES_DENORMALIZE={})
    def test_a_copied_column_whose_name_ends_in_id_is_recognised(self):
        """
        Case: Parent has the plain column external_id. Leaf copies parent__external_id, and its Meta.ordering is
              ['parent__external_id'].
        Expected: One warning W201, which names the entry 'parent__external_id'.
        """

        class Parent(models.Model):
            external_id = models.CharField(max_length=20)

        class Leaf(models.Model):
            parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
            parent_external_id = DenormalizedField('parent__external_id')

            class Meta:
                ordering = ['parent__external_id']

        messages = check_ordering_uses_copies(app_configs=self.apps.get_app_configs())

        self.assertEqual([message.id for message in messages], ['postgres_denormalize.W201'])
        self.assertIn('parent__external_id', messages[0].msg)
        self.assertEqual(messages[0].hint, 'Order by `parent_external_id` instead.')


@isolate_apps('isolated')
class OwnersOutsideTheProjectTestCase(SimpleTestCase):
    def test_a_hop_to_a_model_of_an_installed_package_is_a_warning(self):
        """
        Case: Leaf copies author__email, where author is a foreign key to auth.User, whose app is installed in
              site-packages. Proxied copies author__email too, through a foreign key to a proxy model of auth.User in
              the isolated app.
        Expected: One warning W302 for Leaf, which names auth.User and whose hint suggests a proxy model. Proxied has no
                  warning.
        """

        class Leaf(models.Model):
            author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')
            author_email = DenormalizedField('author__email')

        class Author(User):
            class Meta:
                proxy = True

        class Proxied(models.Model):
            author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name='+')
            author_email = DenormalizedField('author__email')

        messages = third_party_owner_warnings([Leaf, Proxied])

        self.assertEqual([(message.id, message.obj) for message in messages], [('postgres_denormalize.W302', Leaf)])
        self.assertIn('auth.User', messages[0].msg)
        self.assertIn('proxy model', messages[0].hint)
        self.assertNotIn('PGTRIGGER', messages[0].hint)
