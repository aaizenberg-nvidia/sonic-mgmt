"""

conftest.py

Defines the methods and fixtures which will be used by pytest

"""

import pytest


def pytest_addoption(parser: pytest.Parser):
    """
    Parse pytest options
    :param parser: pytest builtin
    """
    cc = parser.getgroup("Code coverage")
    cc.addoption('--dest', action='store', required=True, help='The directory in which to save the extracted coverage .xml files')
    cc.addoption("--nvos", action="store_true", default=False, help="Run on NVOS system (IB)")
    cc.addoption("--cc-branch", help="The branch name")
    cc.addoption("--cc-commit-id", help="The commit id")


def pytest_configure(config: pytest.Config):
    branch_name = config.getoption('--cc-branch')
    commit_id = config.getoption('--cc-commit-id')
    if (branch_name is None) != (commit_id is None):
        raise pytest.UsageError("--cc-branch and --cc-commit-id must be provided together")


@pytest.fixture(scope='module')
def dest(request: pytest.FixtureRequest):
    return request.config.getoption('--dest')


@pytest.fixture(scope='module')
def nvos(request: pytest.FixtureRequest):
    return request.config.getoption('--nvos')


@pytest.fixture
def branch_n_commit_id(request: pytest.FixtureRequest):
    if not (branch_name := request.config.getoption('--cc-branch')):
        return
    if not (commit_id := request.config.getoption('--cc-commit-id')):
        return

    return branch_name, commit_id
