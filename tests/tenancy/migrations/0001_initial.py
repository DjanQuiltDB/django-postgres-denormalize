import django.db.models.deletion
import pgtrigger.compiler
import pgtrigger.migrations
from django.db import migrations, models

import postgres_denormalize.fields


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Project',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('code', models.CharField(blank=True, max_length=20, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='Tenant',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
            ],
        ),
        migrations.CreateModel(
            name='Task',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=100)),
                ('archived', models.BooleanField(default=False)),
                (
                    'project_name',
                    postgres_denormalize.fields.DenormalizedField(
                        base_field=models.CharField(max_length=100), denormalize_from='project__name'
                    ),
                ),
                (
                    'project_code',
                    postgres_denormalize.fields.DenormalizedField(
                        base_field=models.CharField(max_length=20, null=True), denormalize_from='project__code'
                    ),
                ),
                (
                    'project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name='tasks', to='tenancy.project'
                    ),
                ),
                (
                    'tenant',
                    postgres_denormalize.fields.DenormalizedForeignKey(
                        denormalize_from='project__tenant',
                        null=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='tasks',
                        to='tenancy.tenant',
                    ),
                ),
            ],
            options={
                'ordering': ['project_name', 'title'],
            },
        ),
        migrations.AddField(
            model_name='project',
            name='tenant',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name='projects', to='tenancy.tenant'
            ),
        ),
        migrations.CreateModel(
            name='Comment',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('body', models.TextField()),
                (
                    'task',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name='comments', to='tenancy.task'
                    ),
                ),
                (
                    'tenant',
                    postgres_denormalize.fields.DenormalizedForeignKey(
                        denormalize_from='task__project__tenant',
                        null=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='comments',
                        to='tenancy.tenant',
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name='task',
            index=models.Index(
                condition=models.Q(('archived', False)),
                fields=['tenant', 'project_name', 'title'],
                name='tenancy_task_listing',
            ),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.UniqueConstraint(
                condition=models.Q(('archived', False)),
                fields=('tenant', 'title'),
                name='tenancy_task_unique_title_per_tenant',
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='task',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_cascade_tenancy_comment',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    condition='WHEN (OLD."project_id" IS DISTINCT FROM NEW."project_id")',
                    func='UPDATE "tenancy_comment" leaf\nSET "tenant_id" = (SELECT upper0."tenant_id"\n FROM "tenancy_project" upper0\n WHERE upper0."id" = NEW."project_id")\nWHERE leaf."task_id" = NEW."id"\n  AND leaf."tenant_id" IS DISTINCT FROM (SELECT upper0."tenant_id"\n FROM "tenancy_project" upper0\n WHERE upper0."id" = NEW."project_id");\nRETURN NULL;',
                    hash='91c54186613c5aedaa7a704c512a4afd5557bdf8',
                    operation='UPDATE',
                    pgid='pgtrigger_denormalize_cascade_tenancy_comment_2f7a8',
                    table='tenancy_task',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='task',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_fill_tenancy_comment',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    func='UPDATE "tenancy_comment" leaf\nSET "tenant_id" = (SELECT upper0."tenant_id"\n FROM "tenancy_project" upper0\n WHERE upper0."id" = NEW."project_id")\nWHERE leaf."task_id" = NEW."id"\n  AND leaf."tenant_id" IS DISTINCT FROM (SELECT upper0."tenant_id"\n FROM "tenancy_project" upper0\n WHERE upper0."id" = NEW."project_id");\nRETURN NULL;',
                    hash='0f8e0d538d259bdc0328e630ee4e7f2d8bf5f4c5',
                    operation='INSERT',
                    pgid='pgtrigger_denormalize_fill_tenancy_comment_b1d5b',
                    table='tenancy_task',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='task',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_set',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    declare='DECLARE ancestor0 record;',
                    func='IF TG_OP = \'UPDATE\' THEN\n    IF OLD."project_id" IS NOT DISTINCT FROM NEW."project_id"\n        AND OLD."tenant_id" IS NOT DISTINCT FROM NEW."tenant_id"\n        AND OLD."project_name" IS NOT DISTINCT FROM NEW."project_name"\n        AND OLD."project_code" IS NOT DISTINCT FROM NEW."project_code" THEN\n        RETURN NEW;\n    END IF;\nEND IF;\nSELECT hop0."tenant_id" AS value0, hop0."name" AS value1, hop0."code" AS value2\nINTO ancestor0\nFROM "tenancy_project" hop0\nWHERE hop0."id" = NEW."project_id";\nIF FOUND THEN\n    NEW."tenant_id" := ancestor0.value0;\n    NEW."project_name" := ancestor0.value1;\n    NEW."project_code" := ancestor0.value2;\nEND IF;\nRETURN NEW;',
                    hash='b306ec34dd6571f913fff6479090758893280694',
                    operation='INSERT OR UPDATE',
                    pgid='pgtrigger_denormalize_set_a2dd6',
                    table='tenancy_task',
                    when='BEFORE',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='project',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_cascade_tenancy_comment',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    condition='WHEN (OLD."tenant_id" IS DISTINCT FROM NEW."tenant_id")',
                    func='UPDATE "tenancy_comment" leaf\nSET "tenant_id" = NEW."tenant_id"\nFROM "tenancy_task" hop0\nWHERE leaf."task_id" = hop0."id"\n  AND hop0."project_id" = NEW."id"\n  AND leaf."tenant_id" IS DISTINCT FROM NEW."tenant_id";\nRETURN NULL;',
                    hash='8ee3b0b222559fc9a6ed77f55e72bfb51755fc65',
                    operation='UPDATE',
                    pgid='pgtrigger_denormalize_cascade_tenancy_comment_215eb',
                    table='tenancy_project',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='project',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_fill_tenancy_comment',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    func='UPDATE "tenancy_comment" leaf\nSET "tenant_id" = NEW."tenant_id"\nFROM "tenancy_task" hop0\nWHERE leaf."task_id" = hop0."id"\n  AND hop0."project_id" = NEW."id"\n  AND leaf."tenant_id" IS DISTINCT FROM NEW."tenant_id";\nRETURN NULL;',
                    hash='4bbe61e26e367f8fb889ba6751eccd3af625f098',
                    operation='INSERT',
                    pgid='pgtrigger_denormalize_fill_tenancy_comment_76043',
                    table='tenancy_project',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='project',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_cascade_tenancy_task',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    condition='WHEN (OLD."tenant_id" IS DISTINCT FROM NEW."tenant_id" OR OLD."name" IS DISTINCT FROM NEW."name" OR OLD."code" IS DISTINCT FROM NEW."code")',
                    func='UPDATE "tenancy_task" leaf\nSET "tenant_id" = NEW."tenant_id",\n    "project_name" = NEW."name",\n    "project_code" = NEW."code"\nWHERE leaf."project_id" = NEW."id"\n  AND (leaf."tenant_id" IS DISTINCT FROM NEW."tenant_id"\n    OR leaf."project_name" IS DISTINCT FROM NEW."name"\n    OR leaf."project_code" IS DISTINCT FROM NEW."code");\nRETURN NULL;',
                    hash='c2180c2f6da5dc0d227421ef76eb58017f381bd2',
                    operation='UPDATE',
                    pgid='pgtrigger_denormalize_cascade_tenancy_task_de430',
                    table='tenancy_project',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='project',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_fill_tenancy_task',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    func='UPDATE "tenancy_task" leaf\nSET "tenant_id" = NEW."tenant_id",\n    "project_name" = NEW."name",\n    "project_code" = NEW."code"\nWHERE leaf."project_id" = NEW."id"\n  AND (leaf."tenant_id" IS DISTINCT FROM NEW."tenant_id"\n    OR leaf."project_name" IS DISTINCT FROM NEW."name"\n    OR leaf."project_code" IS DISTINCT FROM NEW."code");\nRETURN NULL;',
                    hash='ceb5f3ad5167de0316532a7eb602dcb060988036',
                    operation='INSERT',
                    pgid='pgtrigger_denormalize_fill_tenancy_task_a1883',
                    table='tenancy_project',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='comment',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_set',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    declare='DECLARE ancestor0 record;',
                    func='IF TG_OP = \'UPDATE\' THEN\n    IF OLD."task_id" IS NOT DISTINCT FROM NEW."task_id"\n        AND OLD."tenant_id" IS NOT DISTINCT FROM NEW."tenant_id" THEN\n        RETURN NEW;\n    END IF;\nEND IF;\nSELECT hop1."tenant_id" AS value0\nINTO ancestor0\nFROM "tenancy_task" hop0\nLEFT JOIN "tenancy_project" hop1 ON hop1."id" = hop0."project_id"\nWHERE hop0."id" = NEW."task_id";\nIF FOUND THEN\n    NEW."tenant_id" := ancestor0.value0;\nEND IF;\nRETURN NEW;',
                    hash='5e9137d9380e545b505e1e572190abe5ecf1cda9',
                    operation='INSERT OR UPDATE',
                    pgid='pgtrigger_denormalize_set_db1d9',
                    table='tenancy_comment',
                    when='BEFORE',
                ),
            ),
        ),
    ]
