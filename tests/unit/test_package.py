import taskmanager


def test_version_defined() -> None:
    assert hasattr(taskmanager, "__version__")
    assert isinstance(taskmanager.__version__, str)
