"""Packaging sanity: the app package imports from the installed (editable) distribution."""


def test_package_exposes_version() -> None:
    import vienetts_app

    assert isinstance(vienetts_app.__version__, str)
    assert vienetts_app.__version__.count(".") == 2
