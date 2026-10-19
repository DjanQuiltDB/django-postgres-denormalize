"""
System checks that look at the project as a whole rather than at one field.

The field-level checks (a lookup path that does not resolve, a nullable hop, a target that differs from the path) are
part of each field's own ``check()``. A field on an excluded model skips the field checks that
``EXCLUDE_MODELS_SILENCED_CHECKS`` lists. The checks here skip excluded models altogether. They are registered under the
``postgres_denormalize`` tag:

- ``E301``: pgtrigger neither writes triggers into migrations nor installs them after ``migrate``, so the triggers that
  this package derives would never reach the database.
- ``W302``: a lookup path crosses a model of an installed package, so that model gets triggers, and
  ``makemigrations`` writes them into the package's migrations. A hop that points at a proxy model in one of the
  project's own apps moves the triggers into that app.
- ``W303``/``E303``: a database that stores copies runs transactions below SERIALIZABLE. A leaf write that runs at the
  same time as an update of its ancestor can then commit a copy that differs from its lookup path.
- ``W101``/``E101``: a foreign key whose target the model can also reach through another chain of foreign keys. The
  column then stores a copy that no trigger keeps equal to its source. Declare it as a ``DenormalizedForeignKey``, or
  list it in ``REDUNDANT_COPY_CHECK['ALLOWED']`` with the reason why the two may differ.
- ``E102``: an ``ALLOWED`` entry that does not name such a foreign key, so the entry can be removed.
- ``E103``: an ``ALLOWED`` entry with an empty reason, which cannot be told apart from an oversight.
- ``W201``/``E201``: a ``Meta.ordering`` that walks a lookup path that the model also stores a copy of. Every query
  with the default ordering then joins the tables that the copy is meant to avoid, and an index on the copy cannot
  serve it.
- ``E401``: an ``EXCLUDE_MODELS`` entry that names no installed model, so it excludes nothing.
- ``E402``: a ``REDUNDANT_COPY_CHECK['APPS']`` entry that names no installed app, so it checks nothing.

``REDUNDANT_COPY_CHECK['LEVEL']``, ``ORDERING_CHECK_LEVEL`` and ``ISOLATION_CHECK_LEVEL`` choose between the warning,
the error, and ``'off'``.
"""

import site
import sysconfig
from collections import deque
from pathlib import Path

from django.apps import apps
from django.core import checks
from django.core.exceptions import FieldDoesNotExist
from django.db import connections, router
from django.db.backends.postgresql.psycopg_any import IsolationLevel
from pgtrigger import features

from postgres_denormalize import conf
from postgres_denormalize.chains import can_cross, is_forward_hop
from postgres_denormalize.fields import DenormalizedForeignKey, get_denormalized_fields
from postgres_denormalize.triggers import owners_of, resolvable_fields

TAG = 'postgres_denormalize'


@checks.register(TAG)
def check_triggers_reach_the_database(app_configs=None, **kwargs):
    if features.migrations() or features.install_on_migrate():
        return []
    return [
        checks.Error(
            'pgtrigger neither writes triggers into migrations nor installs them after migrate, so the triggers that '
            'postgres_denormalize derives never reach the database.',
            hint='Leave PGTRIGGER_MIGRATIONS on (the default), or turn PGTRIGGER_INSTALL_ON_MIGRATE on.',
            id='postgres_denormalize.E301',
        )
    ]


@checks.register(TAG)
def check_excluded_models_exist(app_configs=None, **kwargs):
    installed = {model._meta.label_lower for model in apps.get_models()}
    return [
        checks.Error(
            f'{label} is listed in EXCLUDE_MODELS, but no installed model has that label, '
            'so the entry excludes nothing.',
            hint="Use the model's label, 'app_label.ModelName', or remove the entry.",
            id='postgres_denormalize.E401',
        )
        for label in sorted(conf.get_setting('EXCLUDE_MODELS'))
        if label.lower() not in installed
    ]


@checks.register(TAG)
def check_redundant_copies(app_configs=None, **kwargs):
    config = conf.get_setting('REDUNDANT_COPY_CHECK')
    if config['LEVEL'] == 'off':
        return []

    allowed = config['ALLOWED']
    messages = []
    flagged = set()
    installed_labels = {app_config.label for app_config in apps.get_app_configs()}
    for label in sorted(config['APPS'] or ()):
        if label not in installed_labels:
            messages.append(
                checks.Error(
                    f'{label} is listed in REDUNDANT_COPY_CHECK["APPS"], but no installed app has that label, so the '
                    'entry checks nothing.',
                    hint='Use the app label, or remove the entry.',
                    id='postgres_denormalize.E402',
                )
            )
    checked = _checked_models(app_configs, config['APPS'])
    for model in checked:
        for field in _forward_to_one_fields(model):
            path = alternative_paths(model, field, config['MAX_DEPTH']).get(field.related_model)
            if path is None:
                continue
            label = f'{model._meta.label}.{field.name}'
            flagged.add(label)
            if isinstance(field, DenormalizedForeignKey) or label in allowed:
                continue
            messages.append(
                _warning_or_error(
                    config['LEVEL'],
                    '101',
                    f'{label} points at {field.related_model._meta.label}, which {model._meta.label} also reaches '
                    f'through `{path}`, so the column stores a copy that no trigger keeps equal to its source.',
                    hint=(
                        f"Declare it as DenormalizedForeignKey('{path}', ...), or add '{label}' to "
                        f"POSTGRES_DENORMALIZE['REDUNDANT_COPY_CHECK']['ALLOWED'] with the reason the two may differ."
                    ),
                    obj=field,
                )
            )

    checked_labels = {model._meta.label for model in checked}
    for label, reason in sorted(allowed.items()):
        if label.rpartition('.')[0] in checked_labels and label not in flagged:
            messages.append(
                checks.Error(
                    f'{label} is listed in REDUNDANT_COPY_CHECK["ALLOWED"], but its model cannot reach its target in '
                    'any other way, so the entry is not needed.',
                    id='postgres_denormalize.E102',
                )
            )
        if not reason or not str(reason).strip():
            messages.append(
                checks.Error(
                    f'{label} is listed in REDUNDANT_COPY_CHECK["ALLOWED"] without a reason.',
                    hint='State why the two routes may lead to different rows.',
                    id='postgres_denormalize.E103',
                )
            )
    return messages


@checks.register(TAG)
def check_ordering_uses_copies(app_configs=None, **kwargs):
    level = conf.get_setting('ORDERING_CHECK_LEVEL')
    if level == 'off':
        return []

    messages = []
    for model in _checked_models(app_configs, None):
        for entry in model._meta.ordering:
            if not isinstance(entry, str):
                continue
            for field in get_denormalized_fields(model):
                if crosses_lookup_path(model, entry.removeprefix('-').removeprefix('?'), field.denormalize_from):
                    messages.append(
                        _warning_or_error(
                            level,
                            '201',
                            f'{model._meta.label} orders by `{entry}`, which walks `{field.denormalize_from}`, a path '
                            f'that its field `{field.name}` stores a copy of.',
                            hint=f'Order by `{ordering_through_copy(model, entry, field)}` instead.',
                            obj=model,
                        )
                    )
    return messages


def ordering_through_copy(model, entry, field):
    """
    Return the ordering ``entry`` of ``model`` with the lookup path that ``field`` copies replaced by the field.

    The direction and the segments after the copied path are kept: with the copy ``tenant`` of ``project__tenant``,
    ``-project__tenant__name`` becomes ``-tenant__name``.
    """
    direction = '-' if entry.startswith('-') else ''
    names = _field_names(model, entry.removeprefix('-'))
    rest = names[len(field.denormalize_from.split('__')) :]
    return direction + '__'.join([field.name, *rest])


@checks.register(TAG)
def check_owners_outside_the_project(app_configs=None, **kwargs):
    return third_party_owner_warnings(_checked_models(app_configs, None))


def third_party_owner_warnings(leaves):
    """
    Return a W302 warning for every model in a site-packages app that gets cascade and fill triggers for one of
    ``leaves``.

    pgtrigger adds a trigger to the migration state of the model that it is registered on. ``makemigrations`` would then
    write a migration into the third-party app, inside site-packages, or report a change that never goes away. A hop
    that points at a proxy model in the project's own app moves the triggers, and their migration, into that app.
    """
    installed = _site_packages_directories()
    messages = []
    for leaf in leaves:
        for owner in owners_of(resolvable_fields(leaf)):
            config = apps.get_app_config(owner._meta.app_label)
            if not _is_inside(Path(config.path), installed):
                continue
            messages.append(
                checks.Warning(
                    f'{leaf._meta.label} copies from {owner._meta.label}, a model of an installed package, so its '
                    f'cascade and fill triggers are registered on {owner._meta.label}, and makemigrations writes them '
                    f'into the migrations of {config.label}.',
                    hint=(
                        f'Declare a proxy model of {owner._meta.label} in one of your own apps, and point the foreign '
                        f'key of {leaf._meta.label} at the proxy. The triggers and their migration then belong to that '
                        f'app.'
                    ),
                    obj=leaf,
                    id='postgres_denormalize.W302',
                )
            )
    return messages


@checks.register(TAG)
def check_isolation_level(app_configs=None, **kwargs):
    level = conf.get_setting('ISOLATION_CHECK_LEVEL')
    if level == 'off':
        return []
    leaves = [model for model in apps.get_models() if get_denormalized_fields(model) and not conf.is_excluded(model)]
    messages = []
    for alias in connections:
        connection = connections[alias]
        if connection.vendor != 'postgresql':
            continue
        if not any(router.allow_migrate_model(alias, leaf) for leaf in leaves):
            continue
        isolation = connection.settings_dict.get('OPTIONS', {}).get('isolation_level', IsolationLevel.READ_COMMITTED)
        if isolation == IsolationLevel.SERIALIZABLE:
            continue
        try:
            isolation_name = IsolationLevel(isolation).name.replace('_', ' ')
        except ValueError:
            isolation_name = repr(isolation)
        messages.append(
            _warning_or_error(
                level,
                '303',
                f'The database {alias!r} runs transactions at the isolation level {isolation_name}. Below '
                f'SERIALIZABLE, a write to a leaf row that runs at the same time as an update of its ancestor row can '
                f'commit a copy that differs from its lookup path.',
                hint=(
                    f"Set DATABASES[{alias!r}]['OPTIONS']['isolation_level'] to IsolationLevel.SERIALIZABLE, and retry "
                    f'transactions that fail with a serialization error. Or set '
                    f"POSTGRES_DENORMALIZE['ISOLATION_CHECK_LEVEL'] to 'off', and find such copies with "
                    f'drift_condition().'
                ),
            )
        )
    return messages


def crosses_lookup_path(model, path, denormalize_from):
    """
    Say whether ``path``, an ordering of ``model``, walks the whole of ``denormalize_from``.

    For the lookup path ``project__tenant``, the paths ``project__tenant``, ``project__tenant_id`` and
    ``project__tenant__name`` all walk it, and each of them pays for the join that the copy is meant to avoid.
    ``project`` does not walk it, because it ends before the copied value.
    """
    path = '__'.join(_field_names(model, path))
    return path == denormalize_from or path.startswith(f'{denormalize_from}__')


def _field_names(model, path):
    """
    Return the segments of ``path``, with each segment that names a field replaced by the field's name.

    A foreign key's column, such as ``tenant_id``, therefore becomes the name of the foreign key, ``tenant``. A column
    whose own name ends in ``_id`` keeps its name. Once a segment names no field, or a field that is not a relation,
    the remaining segments are transforms or lookups, and they are returned as they are.
    """
    segments = path.split('__')
    names = []
    current = model
    for position, segment in enumerate(segments):
        try:
            field = current._meta.get_field(segment)
        except FieldDoesNotExist:
            return [*names, *segments[position:]]
        names.append(field.name)
        if not field.is_relation or isinstance(field.related_model, str):
            return [*names, *segments[position + 1 :]]
        current = field.related_model
    return names


def alternative_paths(model, excluded_first_hop, max_depth):
    """
    Map each model that can be reached from ``model`` in two or more hops to one lookup path that reaches it.

    The walk never starts with ``excluded_first_hop``, so a field never counts as an alternative route to itself. A
    single hop does not make a field redundant: two foreign keys on the same model that point at the same target are
    siblings, not copies. The walk crosses only the hops that a lookup path may cross (see ``can_cross``), so every
    path that it returns is a path that a ``DenormalizedForeignKey`` accepts.

    The walk is breadth-first, so each model is first reached, and expanded, at its smallest depth. A depth-first walk
    could first reach a model through a long route and stop expanding it at the depth limit. It would then never
    revisit the model through a shorter route, and miss every model that only the shorter route brings within the limit.
    """
    paths = {}
    visited = {model}
    frontier = deque([(model, 0, '')])

    while frontier:
        current, depth, prefix = frontier.popleft()
        if depth >= max_depth:
            continue
        for field in current._meta.get_fields():
            if not can_cross(current, field) or (current is model and field is excluded_first_hop):
                continue
            path = f'{prefix}{field.name}'
            if depth >= 1:
                paths.setdefault(field.related_model, path)
            if field.related_model not in visited:
                visited.add(field.related_model)
                frontier.append((field.related_model, depth + 1, f'{path}__'))

    return paths


def _forward_to_one_fields(model):
    """
    Return the model's own foreign keys and one-to-one fields, which are the only relations that lead to a single row.

    A field that the model inherits from a concrete parent is not its own: its column is in the parent's table, and the
    check looks at it on the parent.
    """
    return [field for field in model._meta.local_fields if is_forward_hop(field)]


def _checked_models(app_configs, labels):
    """
    Return the models that the project-wide checks look at.

    ``app_configs`` is the list of apps that ``manage.py check <app_label>`` limits the run to. Without it, ``labels``
    names the apps. Without those, every app that is not installed in a site-packages directory is checked, because the
    project cannot change the models of a third-party app. Proxy models are skipped, because their fields belong to the
    model that they proxy. Excluded models are skipped too.
    """
    if app_configs is not None:
        configs = app_configs
    elif labels is not None:
        # A label that names no installed app is skipped here. check_redundant_copies reports it as E402.
        installed = {config.label: config for config in apps.get_app_configs()}
        configs = [installed[label] for label in labels if label in installed]
    else:
        installed = _site_packages_directories()
        configs = [config for config in apps.get_app_configs() if not _is_inside(Path(config.path), installed)]

    return [
        model
        for config in configs
        for model in config.get_models()
        if not model._meta.proxy and not conf.is_excluded(model)
    ]


def _site_packages_directories():
    candidates = [*site.getsitepackages(), site.getusersitepackages()]
    candidates.extend(sysconfig.get_paths()[key] for key in ('purelib', 'platlib'))
    return {Path(candidate).resolve() for candidate in candidates}


def _is_inside(path, directories):
    resolved = path.resolve()
    return any(directory == resolved or directory in resolved.parents for directory in directories)


def _warning_or_error(level, number, message, **kwargs):
    if level == 'error':
        return checks.Error(message, id=f'postgres_denormalize.E{number}', **kwargs)
    return checks.Warning(message, id=f'postgres_denormalize.W{number}', **kwargs)


__all__ = ['alternative_paths', 'crosses_lookup_path']
