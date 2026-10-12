"""
Models that make the lookup paths hard to resolve.

Item is declared before the models that its lookup paths cross, and names them by lazy string references, including
relative ones, so inferring a target has to wait for each model to load. Its tenant comes from another app, and its
parent category comes through a self-referential, nullable hop, and its copy infers both ``on_delete`` (SET_NULL) and
``null``. One of its copies passes ``to`` explicitly. ItemAudit
is an audit table that EXCLUDE_MODELS excludes: it has the same denormalized field, but the field is not kept in sync.

Review copies the email of its reviewer from auth.User, a model of an installed package. Its foreign key points at
Reviewer, a proxy model of auth.User in this app, so the triggers on the users table belong to Reviewer, and their
migration is in this app.
"""

from django.contrib.auth.models import User
from django.db import models

from postgres_denormalize.fields import DenormalizedField, DenormalizedForeignKey


class Item(models.Model):
    category = models.ForeignKey('Category', on_delete=models.CASCADE, related_name='items')
    name = models.CharField(max_length=100)
    # A section that is chosen independently. It is listed in REDUNDANT_COPY_CHECK['ALLOWED'] because it is not a copy.
    home_section = models.ForeignKey('catalog.Section', on_delete=models.CASCADE, related_name='homed_items')
    tenant = DenormalizedForeignKey('category__section__tenant', on_delete=models.CASCADE, related_name='items')
    section = DenormalizedForeignKey(
        'category__section', on_delete=models.CASCADE, to='catalog.Section', related_name='listed_items'
    )
    parent_category = DenormalizedForeignKey('category__parent', related_name='grandchild_items')
    section_title = DenormalizedField('category__section__title')


class Category(models.Model):
    section = models.ForeignKey('Section', on_delete=models.CASCADE, related_name='categories')
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='children')
    label = models.CharField(max_length=50)


class Section(models.Model):
    tenant = models.ForeignKey('tenancy.Tenant', on_delete=models.CASCADE, related_name='sections')
    title = models.CharField(max_length=80)


class ItemAudit(models.Model):
    item = models.ForeignKey(Item, on_delete=models.DO_NOTHING, db_constraint=False, related_name='audits')
    category = models.ForeignKey(Category, on_delete=models.DO_NOTHING, db_constraint=False, related_name='+')
    section_title = DenormalizedField('category__section__title')


class Reviewer(User):
    class Meta:
        proxy = True


class Review(models.Model):
    reviewer = models.ForeignKey(Reviewer, on_delete=models.CASCADE, related_name='reviews')
    body = models.TextField()
    reviewer_email = DenormalizedField('reviewer__email')
