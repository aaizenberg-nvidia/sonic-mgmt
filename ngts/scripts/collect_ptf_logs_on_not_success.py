import pytest

from devts.infra.tools.general_constants.constants import DefaultTestServerCred
from ngts.tools.sysdumps import collect_ptf_logs


@pytest.mark.disable_loganalyzer
def test_collect_ptf_logs(topology_obj, setup_name, dumps_folder, is_simx, is_air, request):
    if not (is_air or is_simx):
        hyper_engine = topology_obj.players['hypervisor']['engine']
        hyper_engine.username = DefaultTestServerCred.DEFAULT_USERNAME
        hyper_engine.password = DefaultTestServerCred.DEFAULT_PASS
        collect_ptf_logs(
            hyper_engine,
            dumps_folder,
            setup_name,
            getattr(request.config.option, 'testbed_file', None)
        )
