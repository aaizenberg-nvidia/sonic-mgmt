from typing import List, Optional

import pytest

from ngts.nvos_constants.constants_nvos import UserRole
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.helpers import cleanup_aaa_users, set_new_random_users, \
    delete_all_imported_cas, delete_all_imported_certs
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tools.test_utils import allure_utils as allure


@pytest.fixture
def local_admin_users() -> List[UserInfo]:
    users: List[UserInfo] = set_new_random_users(5, UserRole.ADMIN, True)
    return users


@pytest.fixture
def local_monitor_users() -> List[UserInfo]:
    users: List[UserInfo] = set_new_random_users(5, UserRole.MONITOR, True)
    return users


def cleanup_spiffe(users: Optional[List[UserInfo]] = None):
    with allure.step('cleanup spiffe test'):
        System().api.unset(apply=True).verify_result()
        cleanup_aaa_users(users)
        delete_all_imported_cas()
        delete_all_imported_certs()


@pytest.fixture(scope='session', autouse=True)
def cleanup_certs():
    cleanup_spiffe()
    yield
    cleanup_spiffe()
