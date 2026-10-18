from django.core.exceptions import FieldDoesNotExist
from django.test import SimpleTestCase
from tenancy.models import Comment, Task

from postgres_denormalize.chains import resolve_chain


class ResolveChainTestCase(SimpleTestCase):
    def test_returns_every_foreign_key_that_the_path_crosses(self):
        """
        Case: The lookup path task__project__tenant is resolved from Comment.
        Expected: The foreign keys task, project and tenant, on Comment, Task and Project.
        """
        chain = resolve_chain(Comment, 'task__project__tenant')

        self.assertEqual([field.name for field in chain], ['task', 'project', 'tenant'])
        self.assertEqual([field.model.__name__ for field in chain], ['Comment', 'Task', 'Project'])

    def test_rejects_a_reverse_relation(self):
        """
        Case: The lookup path comments__body, which starts with a reverse relation, is resolved from Task.
        Expected: A ValueError.
        """
        with self.assertRaises(ValueError):
            resolve_chain(Task, 'comments__body')

    def test_rejects_a_segment_that_names_no_field(self):
        """
        Case: The lookup path project__nonexistent is resolved from Task.
        Expected: A FieldDoesNotExist error.
        """
        with self.assertRaises(FieldDoesNotExist):
            resolve_chain(Task, 'project__nonexistent')

    def test_accepts_a_column_at_the_end_only_when_asked_to(self):
        """
        Case: The lookup path project__name, which ends at a plain column, is resolved from Task, first without and then
              with allow_column_terminal.
        Expected: Without it, a ValueError. With it, the fields project and name.
        """
        with self.assertRaises(ValueError):
            resolve_chain(Task, 'project__name')

        chain = resolve_chain(Task, 'project__name', allow_column_terminal=True)

        self.assertEqual([field.name for field in chain], ['project', 'name'])

    def test_rejects_a_column_before_the_end(self):
        """
        Case: The lookup path project__name__tenant, which crosses the plain column name before its end, is resolved
              from Task with allow_column_terminal.
        Expected: A ValueError.
        """
        with self.assertRaises(ValueError):
            resolve_chain(Task, 'project__name__tenant', allow_column_terminal=True)
