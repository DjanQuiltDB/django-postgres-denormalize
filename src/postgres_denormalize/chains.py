"""
Walking a lookup path through a model's foreign keys, which both ``fields`` and ``triggers`` need.
"""


class PathRefused(ValueError):
    """
    A lookup path that names only fields that exist, but crosses a field that no lookup path may cross. ``code`` is the
    id of the system check that reports it, and ``hint`` names the path to use instead.
    """

    def __init__(self, message, *, code, hint):
        super().__init__(message)
        self.code = code
        self.hint = hint


def is_forward_hop(field):
    """
    Say whether ``field`` is a hop that a lookup path can cross: a concrete foreign key or one-to-one field, which
    leads to exactly one row.
    """
    return field.concrete and (field.many_to_one or field.one_to_one)


def refuse_hop(model, field, path, position):
    """
    Raise ``PathRefused`` when the segment of ``path`` at ``position``, which names ``field`` on ``model``, is a field
    that no lookup path may cross or end at.

    A denormalized field is refused, because its own value comes from a trigger. A path through it would need that
    trigger to run before the trigger of the path, and a ``DenormalizedForeignKey`` along the path may not have its
    target yet. The hint names the path that the copy itself walks.

    A field that ``model`` inherits from a concrete parent is refused, because its column is in the parent's table and
    not in the table of ``model``. The hint names the path through the parent links.
    """
    segments = path.split('__')
    copied = getattr(field, 'denormalize_from', None)
    if copied is not None:
        suggestion = '__'.join([*segments[:position], copied, *segments[position + 1 :]])
        raise PathRefused(
            f'`{path}` crosses `{field.name}`, which is itself a copy of `{copied}`',
            code='postgres_denormalize.E006',
            hint=f"Copy from '{suggestion}' instead.",
        )
    if field.model._meta.concrete_model is not model._meta.concrete_model:
        suggestion = '__'.join([*segments[:position], *parent_link_names(model, field.model), *segments[position:]])
        raise PathRefused(
            f'`{path}` crosses `{field.name}`, which {model._meta.label} inherits from {field.model._meta.label}, '
            f'so the column is in the table of {field.model._meta.label}',
            code='postgres_denormalize.E007',
            hint=f"Copy from '{suggestion}' instead.",
        )


def can_cross(model, field):
    """
    Say whether a lookup path from ``model`` may cross ``field`` as a hop: ``is_forward_hop`` accepts it, and
    ``refuse_hop`` does not refuse it.
    """
    if not is_forward_hop(field):
        return False
    try:
        refuse_hop(model, field, field.name, 0)
    except PathRefused:
        return False
    return True


def parent_link_names(model, ancestor):
    """
    Return the names of the parent links that lead from ``model`` to its concrete parent ``ancestor``.
    """
    names = []
    while model._meta.concrete_model is not ancestor._meta.concrete_model:
        link = model._meta.get_ancestor_link(ancestor)
        names.append(link.name)
        model = link.related_model
    return names


def resolve_chain(model, path, allow_column_terminal=False):
    """
    Walk a lookup path from ``model`` and return the field that each segment names, starting with the first hop.

    Every segment except the last must be a hop that ``is_forward_hop`` accepts. The last segment must be one too,
    unless ``allow_column_terminal`` is set. In that case, it may also be a concrete column that is not a relation: the
    column that a ``DenormalizedField`` copies.

    Raises ``FieldDoesNotExist`` if a segment names no field on the model that it is resolved against. Raises
    ``ValueError`` if a segment names a field that the walk cannot cross: a reverse accessor, a many-to-many, or a plain
    column anywhere except at the end. Walking one of those yields either a set of rows or no row at all, and the
    trigger needs exactly one value to copy. Raises ``PathRefused``, which is a ``ValueError``, for the fields that
    ``refuse_hop`` refuses.
    """
    segments = path.split('__')
    chain = []
    current = model
    for position, segment in enumerate(segments):
        field = current._meta.get_field(segment)
        is_last = position == len(segments) - 1
        if is_forward_hop(field):
            refuse_hop(current, field, path, position)
            if isinstance(field.related_model, str):
                raise ValueError(
                    f'`{path}` traverses `{segment}`, which points at {field.related_model}, '
                    'a model that is not installed'
                )
            chain.append(field)
            current = field.related_model
            continue
        if is_last and allow_column_terminal and field.concrete and not field.is_relation:
            refuse_hop(current, field, path, position)
            chain.append(field)
            continue
        raise ValueError(f'`{path}` traverses `{segment}`, which is not a forward foreign key')
    return chain


__all__ = ['PathRefused', 'can_cross', 'is_forward_hop', 'refuse_hop', 'resolve_chain']
