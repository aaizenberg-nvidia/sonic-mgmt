import random

from ngts.nvos_constants.constants_nvos import SystemConsts, ApiType
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool


class Version(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/version')
        self.image = BaseComponent(self, path='/image')
        self.packages = Packages(self)

    def get_nvos_image_version(self):
        return OutputParsingTool.parse_json_str_to_dictionary(self.image.show()).get_returned_value()[
            SystemConsts.VERSION_BUILD_ID]

    def get_running_version(self, dut_engine=None) -> str:
        """
        Single-line identifier of the version running on the switch, for reports (e.g. allure environment).

        The 'image' field of `nv show system version` is a nested object holding the build id
        (e.g. {'build-date': 'Tue Sep 8 16:35:17 UTC 2026', 'build-id': 'nvos-25.03.0758-001'}),
        but older images report it as a plain string - both shapes are handled.

        :param dut_engine: engine to run the show command with. Default: the current dut engine.
        :return: the running image build id (e.g. 'nvos-25.03.0758-001'), falling back to
                 'product-release' (Cumulus images don't report 'image'),
                 or an empty string when neither field exists.
        """
        version_fields = OutputParsingTool.parse_json_str_to_dictionary(
            self.show(dut_engine=dut_engine)).get_returned_value()
        image = version_fields.get(SystemConsts.VERSION_IMAGE)
        if isinstance(image, dict):
            image = image.get(SystemConsts.VERSION_BUILD_ID)
        return image or version_fields.get(SystemConsts.VERSION_PRODUCT_RELEASE, '')


class Packages(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/packages')
        self.installed = BaseComponent(self, path='/installed')
