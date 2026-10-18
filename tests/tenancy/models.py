"""
A tenant has projects, a project has tasks, and a task has comments. Every task and comment stores a copy of its tenant,
so queries can scope by tenant without joins, and a unique constraint can include the tenant. Every task also stores
copies of its project's name and code, so a tenant's tasks can be listed and indexed by project without a join.
"""

from django.db import models

from postgres_denormalize.fields import DenormalizedField, DenormalizedForeignKey


class Tenant(models.Model):
    name = models.CharField(max_length=100)


class Project(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='projects')
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20, null=True, blank=True)


class Task(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='tasks')
    title = models.CharField(max_length=100)
    archived = models.BooleanField(default=False)
    tenant = DenormalizedForeignKey('project__tenant', on_delete=models.CASCADE, related_name='tasks')
    project_name = DenormalizedField('project__name')
    project_code = DenormalizedField('project__code')

    class Meta:
        ordering = ['project_name', 'title']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'title'],
                condition=models.Q(archived=False),
                name='tenancy_task_unique_title_per_tenant',
            ),
        ]
        indexes = [
            models.Index(
                fields=['tenant', 'project_name', 'title'],
                condition=models.Q(archived=False),
                name='tenancy_task_listing',
            ),
        ]


class Comment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='comments')
    body = models.TextField()
    # Unlike Task.tenant, this copy infers its on_delete rule (CASCADE) from the path.
    tenant = DenormalizedForeignKey('task__project__tenant', related_name='comments')
