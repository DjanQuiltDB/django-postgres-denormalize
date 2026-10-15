from django.apps import AppConfig


class PostgresDenormalizeConfig(AppConfig):
    name = 'postgres_denormalize'
    verbose_name = 'PostgreSQL denormalize'

    def ready(self):
        from postgres_denormalize import checks, conf  # noqa: F401 -- importing checks registers them.
        from postgres_denormalize.fields import resolve_denormalized_fields
        from postgres_denormalize.triggers import register_triggers

        conf.validate_settings()
        # Every model is loaded by the time any AppConfig.ready() runs, so every lookup path resolves here. The column
        # type of each DenormalizedField is inferred first, because Django reads `null` when it builds queries. Then the
        # triggers are derived from the fields and registered with pgtrigger, which adds them to the migration state.
        resolve_denormalized_fields()
        register_triggers()
