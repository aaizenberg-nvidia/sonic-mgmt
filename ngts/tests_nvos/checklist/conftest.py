import pytest
import logging

from ngts.nvos_tools.platform.Platform import Platform
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.fixture(scope='function')
def platform_component_with_clear(request, devices):
    platform_component_name = request.param
    if platform_component_name == 'bmc' and not getattr(devices.dut, 'has_bmc', False):
        # Skip here rather than in the test body: the teardown below reads the
        # component's files off the DUT, and a box with no BMC cannot answer that.
        # A skip after the yield would still run it and surface as a teardown error.
        pytest.skip("Device does not have BMC")
    platform_component = getattr(Platform().firmware, platform_component_name)
    yield platform_component
    with allure.step('delete fetched firmware image files'):
        files = platform_component.files.get_files()
        platform_component.files.delete_files(files_to_delete=files)
