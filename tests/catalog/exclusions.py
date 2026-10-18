def is_audit_model(model):
    """
    An EXCLUDE_MODELS_CALLBACK for the tests. A model whose name ends in ``audit`` records past values of copies, so its
    copies must not be kept in sync.
    """
    return model._meta.model_name.endswith('audit')
