"""Stage frames swapped by the App's stage controller.

Each module exposes one ``StageFrame`` subclass whose ``NAME`` is unique
across the package. ``app.py`` registers them in ``_STAGE_CLASSES`` and
swaps frames via ``App.show_stage(name, **payload)``.
"""
