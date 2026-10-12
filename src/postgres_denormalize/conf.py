"""
The ``POSTGRES_DENORMALIZE`` setting: its defaults, its validation, and the functions through which the rest of the
package reads it.

Every key is optional::

    POSTGRES_DENORMALIZE = {
        # Models that this package skips: no triggers, no drift filter, no backfill. This is for models that have
        # denormalized fields on purpose without keeping them in sync, such as audit tables. A model is excluded when
        # either key says so.
        'EXCLUDE_MODELS': ['tenancy.TaskAudit'],  # model labels, matched case-insensitively
        'EXCLUDE_MODELS_CALLBACK': 'myproject.audit.is_audit_model',  # a callable, or a dotted path to one
    }

The values are read each time they are needed, not once at start-up, so ``override_settings`` in a test takes effect.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

SETTING_NAME = 'POSTGRES_DENORMALIZE'

DEFAULTS = {
    'EXCLUDE_MODELS': (),
    'EXCLUDE_MODELS_CALLBACK': None,
}


def user_settings():
    """
    Return the project's ``POSTGRES_DENORMALIZE`` dict, empty when the project sets none.
    """
    return getattr(settings, SETTING_NAME, None) or {}


def get_setting(key):
    """
    Return the value of one key, falling back to its default.
    """
    return user_settings().get(key, DEFAULTS[key])


def excluded_labels():
    """
    Return the model labels in ``EXCLUDE_MODELS``, in lowercase.
    """
    return {label.lower() for label in get_setting('EXCLUDE_MODELS')}


def exclusion_callback():
    """
    Return the callable that ``EXCLUDE_MODELS_CALLBACK`` names, or None. A dotted path is imported.
    """
    callback = get_setting('EXCLUDE_MODELS_CALLBACK')
    if isinstance(callback, str):
        return import_string(callback)
    return callback


def is_excluded(model):
    """
    Say whether this package skips ``model``: either ``EXCLUDE_MODELS`` lists it, or ``EXCLUDE_MODELS_CALLBACK`` returns
    a true value for it. When neither is configured, no model is excluded.
    """
    if model._meta.label_lower in excluded_labels():
        return True
    callback = exclusion_callback()
    return callback is not None and bool(callback(model))


def validate_settings():
    """
    Raise ``ImproperlyConfigured`` for a ``POSTGRES_DENORMALIZE`` setting that this package cannot use.

    ``PostgresDenormalizeConfig.ready()`` calls this, so a misspelt key stops the project from starting rather than
    being ignored.
    """
    configured = user_settings()
    if not isinstance(configured, dict):
        raise ImproperlyConfigured(f'{SETTING_NAME} must be a dict, not {type(configured).__name__}.')

    unknown = sorted(set(configured) - set(DEFAULTS))
    if unknown:
        raise ImproperlyConfigured(f'{SETTING_NAME} has unknown keys: {", ".join(unknown)}.')

    labels = get_setting('EXCLUDE_MODELS')
    if isinstance(labels, str) or not isinstance(labels, (list, tuple, set, frozenset)):
        raise ImproperlyConfigured(f'{SETTING_NAME}["EXCLUDE_MODELS"] must be a list of model labels.')
    malformed = sorted(label for label in labels if not isinstance(label, str) or label.count('.') != 1)
    if malformed:
        raise ImproperlyConfigured(
            f'{SETTING_NAME}["EXCLUDE_MODELS"] contains {", ".join(map(repr, malformed))}, but each entry must be a '
            f"model label such as 'app_label.ModelName'."
        )

    callback = get_setting('EXCLUDE_MODELS_CALLBACK')
    if isinstance(callback, str):
        try:
            import_string(callback)
        except ImportError as exception:
            raise ImproperlyConfigured(
                f'{SETTING_NAME}["EXCLUDE_MODELS_CALLBACK"] is {callback!r}, which cannot be imported: {exception}'
            ) from exception
    elif callback is not None and not callable(callback):
        raise ImproperlyConfigured(
            f'{SETTING_NAME}["EXCLUDE_MODELS_CALLBACK"] must be a callable or a dotted path to one.'
        )


__all__ = ['DEFAULTS', 'get_setting', 'is_excluded', 'validate_settings']
