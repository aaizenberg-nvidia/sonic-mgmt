import os.path
import json
import pandas as pd
from datetime import datetime
from collections import defaultdict
from ngts.constants.constants import InfraConst
from ngts.constants.performance_constants import PerfConsts, MongoDbConsts, ValidationConsts, PowerConsts, MRCConsts
from infra.tools.exceptions.test_issue import TestIssue
from infra.tools.redmine.redmine_api import is_redmine_issue_active
import logging

logger = logging.getLogger()


def create_performance_db_template(players, session_id, setup_name):
    """
    The function creates a file with the DUT system information

    Args:
        players: setups players fixture
        session_id: Mars session id, i.e, "9438676"
        setup_name: i.e, "nv_performance_mtvr-moose-17"

    Returns:
    the DUT system information template, and write into perf_db.json file
    {
        "testType": "Switch",
        "documentVersion": 0,
        "documentSource": "NV_Performance",
        "regression": true,
        "runDate": "23-02-2025 14:07:40",
        "linkToData": null,
        "dutSystemInformation": {
            "marsSessionId": "9438676",
            "setupName": "nv_performance_mtvr-moose-17",
            "osType": "DVS",
            "chip": "SPECTRUM4",
            "board": "sn5600",
            "sdkVersion": "4.7.3094-003",
            "hwChassisRev": "AJ",
            "modelNumber": "MSN-9N402-00RI-7N0_Ax",
            "hostDetails": "mtvr-moose-17, IP N/A",
            "serialNumber": "MT2443J011Q7",
            "onieVersion": "2023.11-5.3.0012-115200",
            "psid": "MT_0000000955",
            "osVersion": "dvs-os-sonic_4.7.1920_DEV_LK6.1.38_x86_64---2024-09-09 10:43:49"
        },
        result: {},
    }
    """
    cli_object = players['dut']['cli']
    dut_system_information = cli_object.performance.get_dut_system_information(session_id, setup_name)
    performance_db_template = {
        "testType": "Switch",
        "documentVersion": 0,
        "documentSource": "NV_Performance",
        "regression": True,
        "runDate": datetime.now().strftime(MongoDbConsts.TIME_REGEX_FORMAT),
        "linkToData": None,
        "dutSystemInformation": dut_system_information,
        "result": {}
    }
    with open(MongoDbConsts.PERF_MONGO_DB_RESULTS_PATH, 'w') as file:
        json.dump(performance_db_template, file, indent=4)
    return performance_db_template


def add_test_mongo_metadata(testname, metadata_dict):
    """
    Any test maintains a JSON file with the test specific information,
    that later will be stored in the full db file that will be uploaded into mongo db

    Args:
        testname: i.e, test_ar_perf_link_flap[port_repeated_toggle-4096-IPv6]
        metadata_dict: a dictionary with the info to update on the test, i.e., {"timeStamp": "23-02-2025 14:07:40"}

    Returns:
     None
    """
    test_info_path = os.path.join(PerfConsts.REQUIRMENTS_DIR, f"{testname}_info_dump.json")
    if os.path.exists(test_info_path):
        with open(test_info_path, "r+") as f:
            test_specific_values = json.load(f)
            test_specific_values.update(metadata_dict)
        with open(test_info_path, "w") as f:
            json.dump(test_specific_values, f, indent=4)
    else:
        test_specific_values = metadata_dict
        with open(test_info_path, "w") as f:
            json.dump(test_specific_values, f, indent=4)


def create_test_validation_entry_to_db(players, test_name):
    """
    Args:
        players: setups players fixture
        test_name: i.e, test_ar_perf_link_flap[port_repeated_toggle-4096-IPv6]

    Returns:
        create tests validation information entry in the mongo db format,
        and write the update test entry into the test information file
    """
    cli_object = players['dut']['cli']
    test_specific_values = cli_object.performance.get_test_specific_values(test_name)
    validation_json = test_specific_values.pop(MongoDbConsts.VALIDATOR_RESULTS, None)
    ports_group_df = pd.DataFrame(test_specific_values.pop(MongoDbConsts.PORT_GROUP_DF, []))
    os_ports_name_mapping_df = pd.DataFrame(test_specific_values.pop(ValidationConsts.OS_PORTS_NAME_MAPPING_DATAFRAME, []))
    power_total = test_specific_values.pop(MongoDbConsts.POWER_TOTAL, [])
    power_by_collectors_group = test_specific_values.pop(MongoDbConsts.POWER_BY_COLLECTORS, [])
    trimmed_untrimmed_dropped_percentages = test_specific_values.pop(MongoDbConsts.TRIMMED_UNTRIMMED_DROPPED_PERCENTAGES, None)
    if validation_json:
        test_specific_values[MongoDbConsts.VALIDATOR_RESULTS] = restructure_validator_results(validation_json,
                                                                                              ports_group_df,
                                                                                              os_ports_name_mapping_df,
                                                                                              power_total,
                                                                                              power_by_collectors_group,
                                                                                              trimmed_untrimmed_dropped_percentages)

    test_info_path = os.path.join(PerfConsts.REQUIRMENTS_DIR, f"{test_name}_info_dump.json")
    with open(test_info_path, "w") as f:
        json.dump(test_specific_values, f, indent=4)


def restructure_validator_results(validation_json, ports_group_df, os_ports_name_mapping_df, power_total,
                                  power_by_collectors_group, trimmed_untrimmed_dropped_percentages):
    """
    Args:
        validation_json: the JSON from the SDK TrafficValidator
        ports_group_df: a list of dicts with group name for each port
        power_total: a list of dictionaries representing a dataframe with the power information
        power_by_collectors_group: a list of dictionaries representing a dataframe with the power information
        by collectors group

    Returns:
        an updated dict with the validator info for mongo db
    """
    test_validation_to_mongo_db = {}
    test_validation_to_mongo_db[MongoDbConsts.BW_COUTERS_DATA] = get_bw_counters_data(
        validation_json, ports_group_df, os_ports_name_mapping_df, trimmed_untrimmed_dropped_percentages)
    test_validation_to_mongo_db[MongoDbConsts.TC_DATA] = get_tc_and_pg_info(validation_json, ValidationConsts.TC_PG_SAMPLES, ValidationConsts.TC_DATAFRAME)
    test_validation_to_mongo_db[MongoDbConsts.PG_DATA] = get_tc_and_pg_info(validation_json, ValidationConsts.TC_PG_SAMPLES, ValidationConsts.PG_BUFFER_DATAFRAME)
    test_validation_to_mongo_db[MongoDbConsts.TC_LATENCY_DATA] = get_tc_and_pg_info(validation_json, ValidationConsts.TC_LATENCY_SAMPLES, ValidationConsts.TC_LATENCY_DATAFRAME)
    test_validation_to_mongo_db[MongoDbConsts.TEMP_DATA] = restructure_temp(validation_json)
    test_validation_to_mongo_db[MongoDbConsts.POWER_TOTAL] = restructure_power(power_total)
    test_validation_to_mongo_db[MongoDbConsts.POWER_BY_COLLECTORS] = restructure_power(power_by_collectors_group)
    if not test_validation_to_mongo_db[MongoDbConsts.TC_DATA]:
        test_validation_to_mongo_db.pop(MongoDbConsts.TC_DATA)
    return test_validation_to_mongo_db


def restructure_counters(validation_json):
    """
    take the max counter value from all the samples
    Args:
        validation_json: the JSON from the SDK TrafficValidator

    Returns:
        a single df with the counters max values
    """
    counters_samples = validation_json[ValidationConsts.COUNTERS_SAMPLES]
    counters_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
    counters_df_dict = collect_all_samples_into_df_list(counters_samples, ValidationConsts.COUNTERS_DATAFRAME)
    final_df = pd.DataFrame()
    for port_group, df_list in counters_df_dict.items():
        max_df = pd.concat(df_list).groupby(level=0).max()
        counters_df = max_df.rename(columns={col: change_name_to_camel_case(col) for col in max_df.columns})
        final_df = pd.concat([final_df, counters_df])
    return final_df


def change_name_to_camel_case(name):
    components = name.split('_')
    return components[0] + ''.join(x.title() for x in components[1:])


def get_updated_columns_names():
    updated_columns_names_list = [change_name_to_camel_case(counter) for counter in PerfConsts.TOTAL_COUNTERS]
    updated_columns_names_dict = dict(list(zip(PerfConsts.TOTAL_COUNTERS, updated_columns_names_list)))
    return updated_columns_names_dict


def collect_all_samples_into_df_list(samples, sample_df_key, use_port_groups=True):
    """
    Collects all sample data frames from a nested samples dictionary.

    Args:
        samples: dict, mapping sample_id to either a dict of port_groups or a single sample dict.
        sample_df_key: str, key to extract the list of port dicts from each sample.
        use_port_groups: bool, if True, expects samples to be nested by port_group.

    Returns:
        List of pandas DataFrames, each containing the sample data with port group info if applicable.
    """
    df_dict = defaultdict(list)
    default_sample_id = "default"
    for sample_id, sample_or_groups in samples.items():
        if use_port_groups:
            for port_group, sample in sample_or_groups.items():
                df = pd.DataFrame(sample[sample_df_key])
                df_dict[port_group].append(df)
        else:
            df = pd.DataFrame(sample_or_groups[sample_df_key])
            df_dict[default_sample_id].append(df)
    return df_dict


def restructure_bw(validation_json):
    """
    Args:
        validation_json: the JSON from the SDK TrafficValidator

    Returns:
        a single df with the bandwidth avg values based on all the samples
    """
    bw_samples = validation_json[ValidationConsts.BW_SAMPLES]
    bw_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
    df_dict = collect_all_samples_into_df_list(bw_samples, ValidationConsts.BW_DATAFRAME)
    final_df = pd.DataFrame()
    for port_group, df_list in df_dict.items():
        df_result = get_base_df(df_list)
        df_result[ValidationConsts.TX_RATE] = calculate_avg_on_all_samples(df_list, ValidationConsts.TX_RATE)
        df_result[ValidationConsts.RX_RATE] = calculate_avg_on_all_samples(df_list, ValidationConsts.RX_RATE)
        final_df = pd.concat([final_df, df_result])
    return final_df


def get_tc_and_pg_info(validation_json, sample_key, df_key):
    """
    Args:
        validation_json: the JSON from the SDK TrafficValidator

    Returns:
        a single df with the tc avg values based on all the samples
    """
    tc_pg_samples = validation_json.get(sample_key, None)
    info = []
    if tc_pg_samples:
        tc_pg_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
        info = get_tc_pg_average_info(tc_pg_samples, df_key)
    return info


def get_tc_pg_average_info(tc_pg_samples, df_key):
    df_dict = collect_all_samples_into_df_list(tc_pg_samples, df_key)
    final_df = pd.DataFrame()
    for port_group, df_list in df_dict.items():
        df_result = get_base_df(df_list)
        for key in ValidationConsts.DATAFRAME_KEYS_DICT[df_key]:
            df_result[key] = calculate_avg_on_all_samples(df_list, key)
        df_result[MongoDbConsts.PORT_GROUP_NAME] = port_group
        final_df = pd.concat([final_df, df_result])
    return final_df.to_dict(orient='records')


def get_base_df(df_list):
    return df_list[0].copy()


def calculate_avg_on_all_samples(df_list, sample_key):
    if not df_list:
        raise TestIssue(f"calculate_avg_on_all_samples: empty df_list for key {sample_key}")
    for df in df_list:
        if df.empty or sample_key not in df.columns:
            raise TestIssue(
                f"calculate_avg_on_all_samples: missing column {sample_key} in a sample dataframe "
                f"(empty={df.empty}); check sensors / power parsing for this platform."
            )
    return sum(df[sample_key] for df in df_list) / len(df_list)


def get_bw_counters_data(validation_json, ports_group_df, os_ports_name_mapping_df,
                         trimmed_untrimmed_dropped_percentages):
    """
    Args:
        validation_json: the JSON from the SDK TrafficValidator
        ports_group_df: a list of dicts with group name for each port
        os_ports_name_mapping_df: a list of dicts with os port name for each port
    Returns:
        all the bandwidth and counters data for each port with the port group
    """
    counters_df = restructure_counters(validation_json)
    bw_df = restructure_bw(validation_json)
    bw_counters_data = pd.merge(counters_df, bw_df, on=ValidationConsts.PORT)
    if not ports_group_df.empty and ValidationConsts.PORT in ports_group_df.columns:
        merged_df = pd.merge(bw_counters_data, ports_group_df, on=ValidationConsts.PORT)
    else:
        merged_df = bw_counters_data
    if not os_ports_name_mapping_df.empty:
        merged_df = pd.merge(merged_df, os_ports_name_mapping_df, on=ValidationConsts.PORT)
    if trimmed_untrimmed_dropped_percentages:
        trimmed_untrimmed_dropped_percentages_df = pd.DataFrame(trimmed_untrimmed_dropped_percentages)
        merged_df = pd.merge(merged_df, trimmed_untrimmed_dropped_percentages_df, on=[ValidationConsts.PORT, ValidationConsts.OS_PORT_NAME], how='left')
    df_as_dict = merged_df.to_dict(orient='records')
    return filter_all_Nan_values(df_as_dict)


def filter_all_Nan_values(df_as_dict):
    """
    Args:
        df_as_dict: a dictionary representing a dataframe

    Returns:
        a dictionary representing a dataframe with all the NaN values filtered out
    """
    for dict_row in df_as_dict:
        keys_to_pop = []
        for key, value in dict_row.items():
            if pd.isna(value):
                keys_to_pop.append(key)
        for key in keys_to_pop:
            dict_row.pop(key)
    return df_as_dict


def restructure_temp(validation_json):
    """
    Args:
        validation_json: the JSON from the SDK TrafficValidator

    Returns:
        a single average temp value based on all the samples
    """
    temperature_samples = validation_json[ValidationConsts.TEMPERATURE_SAMPLES]
    temperature_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
    temp_sum = 0
    for sample_id, temperature_samples_dict in temperature_samples.items():
        temp_sum += int(temperature_samples_dict[ValidationConsts.TEMPERATURE])
    temp_avg = temp_sum / len(temperature_samples)
    return temp_avg


def restructure_power(power_df):
    """
    removes null values from the power dataframe for the value of Total power
    Args:
        power_df: a list of dictionaries representing a dataframe with the power information, i.e,
        [{
            "powerSupply": "DVDD TILES",
            "address": "0x67,0x68,0x69,0x6a",
            "powerWatt": 103.38000000000001
        },
        {
            "powerSupply": "Total Power",
            "address": null,
            "powerWatt": 696.439
        }, ...]

    Returns:
        the updated power dataframe, i.e,
        [{
            "powerSupply": "DVDD TILES",
            "address": "0x67,0x68,0x69,0x6a",
            "powerWatt": 103.38000000000001
        },
        {
            "powerSupply": "Total Power",
            "powerWatt": 696.439
        }, ...]
    """
    for power_supply_dict in power_df:
        for key, value in power_supply_dict.items():
            if key == PowerConsts.POWER_SUPPLY and value == PowerConsts.TOTAL_POWER:
                power_supply_dict.pop(PowerConsts.POWER_SUPPLY_ADDRESS, None)
                power_supply_dict.pop(PowerConsts.POWER_CURRENT, None)
                power_supply_dict.pop(PowerConsts.POWER_VOLTAGE, None)
                break
    return power_df


def add_allure_url_into_perf_test(report_url, test_case_name, is_ipv6):
    """
    updates the allure URL report in the test info
    Args:
        report_url: a url link to the test's allure report
        test_case_name: the pytest name of the test
        is_ipv6: is the test running with the ipv6 flag

    Returns:
        None
    """
    test_name = test_case_name.split("::")[-1]
    class_name = test_case_name.split("::")[-2]
    full_test_name = class_name + "_" + test_name
    ip = InfraConst.IPV6 if is_ipv6 else InfraConst.IPV4
    perf_test_name = full_test_name.replace("]", f"-{ip}]")
    test_info_path = os.path.join(PerfConsts.REQUIRMENTS_DIR, f"{perf_test_name}_info_dump.json")
    if os.path.exists(test_info_path):
        with open(test_info_path, "r+") as f:
            db_json = json.load(f)
            db_json[MongoDbConsts.ALLURE_URL] = report_url
        with open(test_info_path, "w") as f:
            json.dump(db_json, f, indent=4)


def get_perf_test_name(request):
    """
    Args:
        request: pytest request object
        is_ipv6: is the test running with the ipv6 flag, i.e, True

    Returns:
        name of the test including the class and ip flag info,
        i.e, TestSPCXRA_x1Split_800G_test_ar_perf_link_flap[port_repeated_toggle-4096-IPv6]
    """
    test_name = request.node.cls.__name__ + "_" + request.node.name
    return test_name
