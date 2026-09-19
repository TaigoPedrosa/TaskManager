import taskmanager


def test_version_defined():
    assert hasattr(taskmanager, "__version__")
    assert isinstance(taskmanager.__version__, str)
