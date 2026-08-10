from ngts.helpers.performance.performance_db_helpers import add_test_mongo_metadata, get_perf_test_name
import pytest
import logging
import os
from ngts.constants.performance_constants import MongoDbConsts, PerfConsts
from ngts.constants.constants import BugHandlerConst, CliType
from ngts.helpers.performance.performance_setup_helpers import skip_test_on_unsupported_os
from ngts.helpers.performance.traffic_helpers import create_json_traffic_file_with_stream_list, create_json_traffic_stream, dscp_to_tc

logger = logging.getLogger()
TESTS_SCENARIO = "lossy_lossless"


@pytest.fixture(scope='session', autouse=True)
def skip_test_conditionally(players):
    """Run first: session-scoped fixtures in sub-folders should depend on this to avoid expensive setup on NVUE."""
    skip_test_on_unsupported_os(players['dut']['cli'], CliType.NVUE)
    yield


def create_lossy_lossless_json_traffic_file(player_alias, traffic_parameters, json_path, num_lossy_packets, num_lossless_packets, chip_type):
    traffic_parameters["num_packets"] = num_lossy_packets if chip_type != "SPC6" else int(num_lossy_packets * 1.5)
    lossy_stream = create_json_traffic_stream(player_alias, traffic_parameters, f"{player_alias}_lossy_stream",
                                              dscp_to_tc(traffic_parameters["lossy_dscp_value"], 2))

    traffic_parameters["num_packets"] = num_lossless_packets if chip_type != "SPC6" else int(num_lossless_packets * 1.5)
    lossless_stream = create_json_traffic_stream(player_alias, traffic_parameters, f"{player_alias}_lossless_stream",
                                                 dscp_to_tc(traffic_parameters["lossless_dscp_value"], 2))

    create_json_traffic_file_with_stream_list(player_alias, traffic_parameters, json_path,
                                              stream_list=[lossy_stream, lossless_stream])


def get_lossy_lossless_basic_traffic(players, conf_args, num_lossy_packets, num_lossless_packets, chip_type, template_suite="traffic_packets_json_files"):
    traffic_jsons = {}
    pkt_size = PerfConsts.PACKET_SIZE_LIST[0]
    for player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
        player_cli_obj = players[player_alias]['cli']
        json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                                 TESTS_SCENARIO, f"{player_alias}_{TESTS_SCENARIO.replace('/', '_')}_{pkt_size}.json")
        traffic_parameters = player_cli_obj.performance.get_traffic_parameters(scenario=TESTS_SCENARIO,
                                                                               conf_args=conf_args)
        traffic_parameters.update({"lossy_dscp_value": 34, "lossless_dscp_value": 26})

        create_lossy_lossless_json_traffic_file(player_alias=player_alias, traffic_parameters=traffic_parameters,
                                                json_path=json_path, num_lossy_packets=num_lossy_packets,
                                                num_lossless_packets=num_lossless_packets, chip_type=chip_type)
        traffic_jsons[player_alias] = json_path
    return traffic_jsons


@pytest.fixture(scope='function', autouse=True)
def update_test_mongo_metadata(request, port_group_df, scenario_name):
    """
    Fixture to update test metadata in MongoDB.
    Requires scenario_name parameter to be present in the test function.
    """
    test_name = get_perf_test_name(request)
    add_test_mongo_metadata(test_name, {MongoDbConsts.CONF_NAME: scenario_name,
                                        MongoDbConsts.PORT_GROUP_DF: port_group_df})
    yield
