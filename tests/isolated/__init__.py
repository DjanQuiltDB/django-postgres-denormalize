"""
Tests that declare their own models, each in an isolated app registry, so that neither the installed models nor
pgtrigger's registry ever see them. The app has no models of its own; its label is the app label of the models that the
tests declare.
"""
