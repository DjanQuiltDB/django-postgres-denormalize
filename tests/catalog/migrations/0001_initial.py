import django.contrib.auth.models
import django.db.models.deletion
import pgtrigger.compiler
import pgtrigger.migrations
from django.db import migrations, models

import postgres_denormalize.fields


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('tenancy', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Reviewer',
            fields=[],
            options={
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('auth.user',),
            managers=[
                ('objects', django.contrib.auth.models.UserManager()),
            ],
        ),
        migrations.CreateModel(
            name='Category',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(max_length=50)),
                (
                    'parent',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='children',
                        to='catalog.category',
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name='Item',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                (
                    'section_title',
                    postgres_denormalize.fields.DenormalizedField(
                        base_field=models.CharField(max_length=80), denormalize_from='category__section__title'
                    ),
                ),
                (
                    'category',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name='items', to='catalog.category'
                    ),
                ),
                (
                    'parent_category',
                    postgres_denormalize.fields.DenormalizedForeignKey(
                        denormalize_from='category__parent',
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='grandchild_items',
                        to='catalog.category',
                    ),
                ),
                (
                    'tenant',
                    postgres_denormalize.fields.DenormalizedForeignKey(
                        denormalize_from='category__section__tenant',
                        null=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='items',
                        to='tenancy.tenant',
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name='ItemAudit',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                (
                    'section_title',
                    postgres_denormalize.fields.DenormalizedField(
                        base_field=models.CharField(max_length=80), denormalize_from='category__section__title'
                    ),
                ),
                (
                    'category',
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name='+',
                        to='catalog.category',
                    ),
                ),
                (
                    'item',
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name='audits',
                        to='catalog.item',
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name='Section',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=80)),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name='sections', to='tenancy.tenant'
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name='item',
            name='home_section',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name='homed_items', to='catalog.section'
            ),
        ),
        migrations.AddField(
            model_name='item',
            name='section',
            field=postgres_denormalize.fields.DenormalizedForeignKey(
                denormalize_from='category__section',
                null=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='listed_items',
                to='catalog.section',
            ),
        ),
        migrations.AddField(
            model_name='category',
            name='section',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name='categories', to='catalog.section'
            ),
        ),
        migrations.CreateModel(
            name='Review',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('body', models.TextField()),
                (
                    'reviewer_email',
                    postgres_denormalize.fields.DenormalizedField(
                        base_field=models.EmailField(max_length=254), denormalize_from='reviewer__email'
                    ),
                ),
                (
                    'reviewer',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name='reviews', to='catalog.reviewer'
                    ),
                ),
            ],
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='reviewer',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_cascade_catalog_review',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    condition="WHEN ((OLD.\"email\" IS DISTINCT FROM NEW.\"email\") AND NOT COALESCE('pgtrigger_denormalize_cascade_catalog_review_05878' = ANY(string_to_array(current_setting('postgres_denormalize.deferred_cascades', true), ',')), false))",
                    func='UPDATE "catalog_review" leaf\nSET "reviewer_email" = NEW."email"\nWHERE leaf."reviewer_id" = NEW."id"\n  AND leaf."reviewer_email" IS DISTINCT FROM NEW."email";\nRETURN NULL;',
                    hash='c8637b138ad6e6985247638fe60f83d79b12af0b',
                    operation='UPDATE',
                    pgid='pgtrigger_denormalize_cascade_catalog_review_05878',
                    table='auth_user',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='reviewer',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_fill_catalog_review',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    func='UPDATE "catalog_review" leaf\nSET "reviewer_email" = NEW."email"\nWHERE leaf."reviewer_id" = NEW."id"\n  AND leaf."reviewer_email" IS DISTINCT FROM NEW."email";\nRETURN NULL;',
                    hash='486035f6eff8ee1ba220dc015f24301fdee66c51',
                    operation='INSERT',
                    pgid='pgtrigger_denormalize_fill_catalog_review_fe6ec',
                    table='auth_user',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='section',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_cascade_catalog_item',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    condition='WHEN ((OLD."tenant_id" IS DISTINCT FROM NEW."tenant_id" OR OLD."title" IS DISTINCT FROM NEW."title") AND NOT COALESCE(\'pgtrigger_denormalize_cascade_catalog_item_b2546\' = ANY(string_to_array(current_setting(\'postgres_denormalize.deferred_cascades\', true), \',\')), false))',
                    func='UPDATE "catalog_item" leaf\nSET "tenant_id" = NEW."tenant_id",\n    "section_title" = NEW."title"\nFROM "catalog_category" hop0\nWHERE leaf."category_id" = hop0."id"\n  AND hop0."section_id" = NEW."id"\n  AND (leaf."tenant_id" IS DISTINCT FROM NEW."tenant_id"\n    OR leaf."section_title" IS DISTINCT FROM NEW."title");\nRETURN NULL;',
                    hash='c9f47d7cd1c17070679f277a679352954a1ca193',
                    operation='UPDATE',
                    pgid='pgtrigger_denormalize_cascade_catalog_item_b2546',
                    table='catalog_section',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='section',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_fill_catalog_item',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    func='UPDATE "catalog_item" leaf\nSET "tenant_id" = NEW."tenant_id",\n    "section_title" = NEW."title"\nFROM "catalog_category" hop0\nWHERE leaf."category_id" = hop0."id"\n  AND hop0."section_id" = NEW."id"\n  AND (leaf."tenant_id" IS DISTINCT FROM NEW."tenant_id"\n    OR leaf."section_title" IS DISTINCT FROM NEW."title");\nRETURN NULL;',
                    hash='c74abc36e4d8d08d50b422797c4333c682b0e7ab',
                    operation='INSERT',
                    pgid='pgtrigger_denormalize_fill_catalog_item_060e2',
                    table='catalog_section',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='item',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_set',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    declare='DECLARE ancestor0 record; ancestor1 record;',
                    func='IF TG_OP = \'UPDATE\' THEN\n    IF OLD."category_id" IS NOT DISTINCT FROM NEW."category_id"\n        AND OLD."tenant_id" IS NOT DISTINCT FROM NEW."tenant_id"\n        AND OLD."section_id" IS NOT DISTINCT FROM NEW."section_id"\n        AND OLD."parent_category_id" IS NOT DISTINCT FROM NEW."parent_category_id"\n        AND OLD."section_title" IS NOT DISTINCT FROM NEW."section_title" THEN\n        RETURN NEW;\n    END IF;\nEND IF;\nSELECT hop1."tenant_id" AS value0, hop1."title" AS value1\nINTO ancestor0\nFROM "catalog_category" hop0\nLEFT JOIN "catalog_section" hop1 ON hop1."id" = hop0."section_id"\nWHERE hop0."id" = NEW."category_id";\nIF FOUND THEN\n    NEW."tenant_id" := ancestor0.value0;\n    NEW."section_title" := ancestor0.value1;\nEND IF;\nSELECT hop0."section_id" AS value0, hop0."parent_id" AS value1\nINTO ancestor1\nFROM "catalog_category" hop0\nWHERE hop0."id" = NEW."category_id";\nIF FOUND THEN\n    NEW."section_id" := ancestor1.value0;\n    NEW."parent_category_id" := ancestor1.value1;\nEND IF;\nRETURN NEW;',
                    hash='cd510c5abc55216080956e3bde6f6d54720837fa',
                    operation='INSERT OR UPDATE',
                    pgid='pgtrigger_denormalize_set_b4f46',
                    table='catalog_item',
                    when='BEFORE',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='category',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_cascade_catalog_item',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    condition='WHEN ((OLD."section_id" IS DISTINCT FROM NEW."section_id" OR OLD."parent_id" IS DISTINCT FROM NEW."parent_id") AND NOT COALESCE(\'pgtrigger_denormalize_cascade_catalog_item_438c5\' = ANY(string_to_array(current_setting(\'postgres_denormalize.deferred_cascades\', true), \',\')), false))',
                    func='UPDATE "catalog_item" leaf\nSET "tenant_id" = (SELECT upper0."tenant_id"\n FROM "catalog_section" upper0\n WHERE upper0."id" = NEW."section_id"),\n    "section_id" = NEW."section_id",\n    "parent_category_id" = NEW."parent_id",\n    "section_title" = (SELECT upper0."title"\n FROM "catalog_section" upper0\n WHERE upper0."id" = NEW."section_id")\nWHERE leaf."category_id" = NEW."id"\n  AND (leaf."tenant_id" IS DISTINCT FROM (SELECT upper0."tenant_id"\n FROM "catalog_section" upper0\n WHERE upper0."id" = NEW."section_id")\n    OR leaf."section_id" IS DISTINCT FROM NEW."section_id"\n    OR leaf."parent_category_id" IS DISTINCT FROM NEW."parent_id"\n    OR leaf."section_title" IS DISTINCT FROM (SELECT upper0."title"\n FROM "catalog_section" upper0\n WHERE upper0."id" = NEW."section_id"));\nRETURN NULL;',
                    hash='391c81a28f08916299a5ed1d4c0ab7ce96130d6a',
                    operation='UPDATE',
                    pgid='pgtrigger_denormalize_cascade_catalog_item_438c5',
                    table='catalog_category',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='category',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_fill_catalog_item',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    func='UPDATE "catalog_item" leaf\nSET "tenant_id" = (SELECT upper0."tenant_id"\n FROM "catalog_section" upper0\n WHERE upper0."id" = NEW."section_id"),\n    "section_id" = NEW."section_id",\n    "parent_category_id" = NEW."parent_id",\n    "section_title" = (SELECT upper0."title"\n FROM "catalog_section" upper0\n WHERE upper0."id" = NEW."section_id")\nWHERE leaf."category_id" = NEW."id"\n  AND (leaf."tenant_id" IS DISTINCT FROM (SELECT upper0."tenant_id"\n FROM "catalog_section" upper0\n WHERE upper0."id" = NEW."section_id")\n    OR leaf."section_id" IS DISTINCT FROM NEW."section_id"\n    OR leaf."parent_category_id" IS DISTINCT FROM NEW."parent_id"\n    OR leaf."section_title" IS DISTINCT FROM (SELECT upper0."title"\n FROM "catalog_section" upper0\n WHERE upper0."id" = NEW."section_id"));\nRETURN NULL;',
                    hash='ce1c172a95315668417fc8d0816e433addbd2dde',
                    operation='INSERT',
                    pgid='pgtrigger_denormalize_fill_catalog_item_3bea4',
                    table='catalog_category',
                    when='AFTER',
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='review',
            trigger=pgtrigger.compiler.Trigger(
                name='denormalize_set',
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    declare='DECLARE ancestor0 record;',
                    func='IF TG_OP = \'UPDATE\' THEN\n    IF OLD."reviewer_id" IS NOT DISTINCT FROM NEW."reviewer_id"\n        AND OLD."reviewer_email" IS NOT DISTINCT FROM NEW."reviewer_email" THEN\n        RETURN NEW;\n    END IF;\nEND IF;\nSELECT hop0."email" AS value0\nINTO ancestor0\nFROM "auth_user" hop0\nWHERE hop0."id" = NEW."reviewer_id";\nIF FOUND THEN\n    NEW."reviewer_email" := ancestor0.value0;\nEND IF;\nRETURN NEW;',
                    hash='94685f2721ab74bf2cd89b934fb878e91dd18add',
                    operation='INSERT OR UPDATE',
                    pgid='pgtrigger_denormalize_set_24339',
                    table='catalog_review',
                    when='BEFORE',
                ),
            ),
        ),
    ]
