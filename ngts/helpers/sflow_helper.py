import logging
import allure
import re
import time
from datetime import datetime
import json
import random
import os
from retry import retry
from ngts.constants.constants import *
from devts.infra.tools.validations.traffic_validations.scapy.scapy_runner import ScapyChecker

SCAPY_TEMPLATE_DIR = MarsConstants.SONIC_MGMT_DIR + '/ngts/helpers/scapy_send_template.py'

logger = logging.getLogger()


def verify_sflow_configuration(cli_obj, status, **kwargs):
    """
    This method is used to verify sflow configuration
    There is a typical show output

        sFlow Global Information:
          sFlow Admin State:          up
          sFlow Polling Interval:     5
          sFlow AgentID:              default

          2 Collectors configured:
            Name: collector0          IP addr: 50.0.0.2        UDP port: 6343   VRF: default
            Name: collector1          IP addr: 60.0.0.2        UDP port: 6555   VRF: default

    :param cli_obj: cli_obj fixture
    :param status: sflow status
    :param kwargs: arguments
    """
    result = cli_obj.sflow.show_sflow()
    logger.info(f"Verify sflow admin state is {status}")
    assert re.search(fr"sFlow Admin State:\s+{status}", result), f"Sflow Admin State is not {status}"
    if 'polling_interval' in kwargs:
        logger.info(f"Verify sflow polling interval is {kwargs['polling_interval']}")
        assert re.search(fr"sFlow Polling Interval:\s+{kwargs['polling_interval']}",
                         result), f"Sflow Polling Interval is not {kwargs['polling_interval']}"
    if 'agent_id' in kwargs:
        logger.info(f"Verify sflow agent id is {kwargs['agent_id']}")
        assert re.search(fr"sFlow AgentID:\s+{kwargs['agent_id']}",
                         result), f"Sflow Agent Id is not {kwargs['agent_id']}"
    if 'collector' in kwargs:
        collector = kwargs['collector']
        if len(collector) is None:
            logger.info("Verify no sflow collector configured")
            assert re.search("0 Collectors configured", result), " Expected 0 collectors , but collectors are present"
        else:
            logger.info(fr"Verify {len(collector)} sflow collector configured")
            assert re.search(f"{len(collector)} Collectors configured:",
                             result), f"Number of Sflow collectors should be {len(collector)}"
            for col in collector:
                e1 = re.search(
                    fr"Name:\s+{SflowConsts.COLLECTOR[col]['name']}\s+IP addr:\s{SflowConsts.COLLECTOR[col]['ip']}\s+UDP port:\s{SflowConsts.COLLECTOR[col]['port']}",
                    result)
                e2 = re.search(
                    fr"Name:\s+{SflowConsts.COLLECTOR[col]['name']}\s+IP addr:\s{SflowConsts.COLLECTOR[col]['ipv6']}\s+UDP port:\s{SflowConsts.COLLECTOR[col]['port']}",
                    result)
                assert (e1 or e2), f"col {col} is not properly Configured"


def verify_sflow_interface_configuration(cli_obj, interface_name, status, sample_rate):
    """
    This method is used to verify sflow interface status
    :param cli_obj: cli_obj fixture
    :param interface_name: interface_name fixture
    :param status: interface status
    :param sample_rate: sflow interface sampling rate
    """
    logger.info(f"Verify sflow interface {interface_name} status")
    show_sflow_intf = cli_obj.sflow.show_sflow_interface()
    assert re.search(fr"{interface_name}\s+\|\s+{status}\s+\|\s+{sample_rate}",
                     show_sflow_intf), f"Interface {interface_name} is not properly configured"


def verify_hsflowd_ready(engines, collector_ips):
    for ip in collector_ips:
        res = engines.dut.run_cmd(
            f"docker exec -i sflow sh -c "
            f"\"grep -q 'collector={ip}' {SflowConsts.HSFLOWD_AUTOGEN_FILE} && echo exist\""
        )
        if res.strip() != 'exist':
            return False
    return True


@retry(Exception, tries=24, delay=10)
def wait_until_hsflowd_ready(engines, collector_ips):
    logger.info("Waiting for hsflowd collector(s): %s", collector_ips)
    assert verify_hsflowd_ready(engines, collector_ips), (
        f"hsflowd not ready yet for collector(s) {collector_ips}, will check again in 10 seconds; "
        f"check docker exec sflow cat {SflowConsts.HSFLOWD_AUTOGEN_FILE}"
    )


def get_agent_id_from_hsflowd(engines):
    """
    This method is used to get the agent ip selected by hsflowd daemon
    """
    res = engines.dut.run_cmd(f"docker exec -i sflow sh -c 'test -f {SflowConsts.HSFLOWD_AUTOGEN_FILE} && echo exist'")
    assert res == 'exist', f"{SflowConsts.HSFLOWD_AUTOGEN_FILE} file does not exist"
    selected_agent_ip = engines.dut.run_cmd(f"docker exec sflow grep -w 'agentIP' {SflowConsts.HSFLOWD_AUTOGEN_FILE}"
                                            f" | cut -d '=' -f 2")
    assert selected_agent_ip, f"agent variable not found in the {SflowConsts.HSFLOWD_AUTOGEN_FILE} file"
    logger.info(f"hsflowd selected agent ip is: {selected_agent_ip}")
    return selected_agent_ip


@retry(Exception, tries=5, delay=5)
def verify_sflow_sample_agent_id(engines, collector, agent_id_addr):
    """
    This method is used to verify agent id in sflow samples
    :param engines: engines fixture
    :param collector: collector name
    :param agent_id_addr: agent_id IP address
    """
    hb_engine = engines.hb
    count = result = 0
    sflowtool_pid = start_sflowtool_process(engines, collector)
    while count < 10:
        time.sleep(1)
        result = hb_engine.run_cmd(
            f"grep -ia {agent_id_addr} {SflowConsts.COLLECTOR[collector]['sample_file']} | wc -l")
        if int(result) > 0:
            break
        count += 1
    else:
        logger.info(f"Could not find {agent_id_addr} in {SflowConsts.COLLECTOR[collector]['sample_file']}")
        logger.info(f"Debug the content of {SflowConsts.COLLECTOR[collector]['sample_file']}")
        hb_engine.run_cmd(f"ls -l {SflowConsts.COLLECTOR[collector]['sample_file']}")
        hb_engine.run_cmd(f"cat {SflowConsts.COLLECTOR[collector]['sample_file']}")
    kill_sflowtool_process(engines, sflowtool_pid)
    assert int(result) > 0, f"Agent id is not {agent_id_addr}"


def start_sflowtool_process(engines, collector):
    """
    This method is used to start sflowtool
    :param engines: engines fixture
    :param collector: collector name
    :return: sflowtool process id
    """
    hb_engine = engines.hb

    logger.info("Start sflowtool")
    result = hb_engine.run_cmd(
        f"{SflowConsts.SFLOW_TOOL_PRETTY + str(SflowConsts.COLLECTOR[collector]['port'])} > {SflowConsts.COLLECTOR[collector]['sample_file']} &")
    sflowtool_pid = result.split()[-1]
    return sflowtool_pid


def kill_sflowtool_process(engines, process_id=None):
    """
    This method is used to kill all sflowtool process at hb
    :param engines: engines fixture
    :param process_id: sflow process id
    """
    hb_engine = engines.hb
    if process_id:
        logger.info(f"Stop sflowtool {process_id}")
        hb_engine.run_cmd(f'kill {process_id}')
    else:
        logger.info(f"Stop all sflowtool")
        hb_engine.run_cmd("killall sflowtool")


def remove_tmp_sample_file(engines):
    """
    This method is used to delete all the sflowtool sample file
    :param engines: engines fixture
    """
    hb_engine = engines.hb
    logger.info(f"Remove {SflowConsts.COLLECTOR[SflowConsts.COLLECTOR_0]['sample_file']}")
    hb_engine.run_cmd(f"rm -rf {SflowConsts.COLLECTOR[SflowConsts.COLLECTOR_0]['sample_file']}")
    logger.info(f"Remove {SflowConsts.COLLECTOR[SflowConsts.COLLECTOR_1]['sample_file']}")
    hb_engine.run_cmd(f"rm -rf {SflowConsts.COLLECTOR[SflowConsts.COLLECTOR_1]['sample_file']}")


def basic_sflow_config(cli_objects, interfaces):
    """
    Pytest fixture used to configure basic sflow configuration for test function
    :param cli_objects: cli_objects fixture
    :param interfaces: interfaces fixture
    """
    cli_obj = cli_objects.dut
    with allure.step(f"Applying sflow configuration"):
        with allure.step(f"Start feature {SflowConsts.SFLOW_FEATURE_NAME}"):
            cli_obj.sflow.enable_sflow_feature()
            time.sleep(2)
        with allure.step(f"Enable {SflowConsts.SFLOW_FEATURE_NAME} globally"):
            cli_obj.sflow.enable_sflow()
        with allure.step(f"Add collector {SflowConsts.COLLECTOR_0} with udp port {SflowConsts.DEFAULT_UDP}"):
            cli_obj.sflow.add_collector(SflowConsts.COLLECTOR_0, SflowConsts.COLLECTOR_0_IP)
        with allure.step("Disable all sflow interface"):
            cli_obj.sflow.disable_all_sflow_interface()
        with allure.step(f"Enable sflow interface {interfaces.dut_ha_1}"):
            cli_obj.sflow.enable_sflow_interface(interfaces.dut_ha_1)
        with allure.step(f"Enable sflow interface {interfaces.dut_ha_2}"):
            cli_obj.sflow.enable_sflow_interface(interfaces.dut_ha_2)


def basic_sflow_cleanup(engines, cli_objects, interfaces):
    """
    Pytest fixture used to clean-up the basic sflow configuration from basic_sflow_config
    :param engines: engines fixture
    :param cli_objects: cli_objects fixture
    :param interfaces: interfaces fixture
    """
    cli_obj = cli_objects.dut
    with allure.step(f"Performing sflow configuration cleanup"):
        with allure.step(f"Delete collector {SflowConsts.COLLECTOR_0}"):
            cli_obj.sflow.del_collector(SflowConsts.COLLECTOR_0)
        with allure.step("Delete agent id"):
            cli_obj.sflow.del_agent_id()
        with allure.step(f"Disable sflow interface {interfaces.dut_ha_1}"):
            cli_obj.sflow.disable_sflow_interface(interfaces.dut_ha_1)
        with allure.step(f"Disable sflow interface {interfaces.dut_ha_2}"):
            cli_obj.sflow.disable_sflow_interface(interfaces.dut_ha_2)
        with allure.step(f"Disable {SflowConsts.SFLOW_FEATURE_NAME} globally"):
            cli_obj.sflow.disable_sflow()
        with allure.step("Kill all sflowtool process"):
            kill_sflowtool_process(engines)
        with allure.step(f"Stop feature {SflowConsts.SFLOW_FEATURE_NAME}"):
            cli_obj.sflow.disable_sflow_feature()
        with allure.step("Remove sflowtool sample files"):
            remove_tmp_sample_file(engines)
        with allure.step("Delete sflow entries from redis db"):
            for table in SflowConsts.SFLOW_REDIS_GENERAL_TABLES:
                cli_obj.general.delete_redis_table(table)


def analyze_sample(sample_file, interface_name=None, loopback=False):
    """
    This method is used to collect time stamps for a specific interface
    The typical flow sample format is:
        {
         "datagramSourceIP":"20.1.1.1",
         "datagramSize":"152",
         "unixSecondsUTC":"1655386549",
         "localtime":"2022-06-16T13:35:49+0000",
         "datagramVersion":"5",
         "agentSubId":"100000",
         "agent":"240.127.1.1",
         "packetSequenceNo":"1509",
         "sysUpTime":"15736722",
         "samplesInPacket":"1",
         "samples":[{
           "sampleType_tag":"0:1",
           "sampleType":"FLOWSAMPLE",
           "sampleSequenceNo":"650",
           "sourceId":"0:9",
           "meanSkipCount":"500",
           "samplePool":"224672",
           "dropEvents":"0",
           "inputPort":"9",
           "outputPort":"0",
           "elements":[{
             "flowBlock_tag":"0:1",
             "flowSampleType":"HEADER",
             "headerProtocol":"1",
             "sampledPacketSize":"64",
             "strippedBytes":"4",
             "headerLen":"60",
             "headerBytes":"90-0A-84-12-A0-00-9E-7C-24-BA-FB-00-08-00-45-00-00-64-01-00-00-00-40-06-E8-3E-C0-A8-10-01-C0-A8-00-04-04-D2-00-50-00-00-00-00-00-00-00-00-50-02-20-00-F9-2E-00-00-00-00-00-00-00-00",
             "dstMAC":"900a8412a000",
             "srcMAC":"9e7c24bafb00",
             "IPSize":"46",
             "ip.tot_len":"100",
             "srcIP":"192.168.16.1",
             "dstIP":"192.168.0.4",
             "IPProtocol":"6",
             "IPTOS":"0",
             "IPTTL":"64",
             "IPID":"1",
             "TCPSrcPort":"1234",
             "TCPDstPort":"80",
             "TCPFlags":"2"
            }
           ]
          }
         ]
        }

    The typical counter sample format is:
        {
         "datagramSourceIP":"20.1.1.1",
         "datagramSize":"796",
         "unixSecondsUTC":"1655360159",
         "localtime":"2022-06-16T06:15:59+0000",
         "datagramVersion":"5",
         "agentSubId":"100000",
         "agent":"10.210.24.181",
         "packetSequenceNo":"5912",
         "sysUpTime":"7363104",
         "samplesInPacket":"1",
         "samples":[{
           "sampleType_tag":"0:2",
           "sampleType":"COUNTERSSAMPLE",
           "sampleSequenceNo":"368",
           "sourceId":"2:1",
           "elements":[{
             "counterBlock_tag":"0:2001",
             "adaptor_list":[{
               "ifIndex":"203",
               "MACs":"1",
               "mac_list":[
                "900a8412a000"
               ]
              },
              {
               "ifIndex":"2",
               "MACs":"1",
               "mac_list":[
                "08c0ebb9a7a0"
               ]
              },
              {
               "ifIndex":"201",
               "MACs":"1",
               "mac_list":[
                "900a8412a000"
               ]
              },
              {
               "ifIndex":"3",
               "MACs":"1",
               "mac_list":[
                "0242eb61de44"
               ]
              },
              {
               "ifIndex":"204",
               "MACs":"1",
               "mac_list":[
                "e607b94fca69"
               ]
              },
              {
               "ifIndex":"269",
               "MACs":"1",
               "mac_list":[
                "900a8412a000"
               ]
              }
             ]
            },
            {
             "counterBlock_tag":"0:2010",
             "udpInDatagrams":"98419",
             "udpNoPorts":"40",
             "udpInErrors":"0",
             "udpOutDatagrams":"106063",
             "udpRcvbufErrors":"0",
             "udpSndbufErrors":"0",
             "udpInCsumErrors":"0"
            },
            {
             "counterBlock_tag":"0:2009",
             "tcpRtoAlgorithm":"1",
             "tcpRtoMin":"200",
             "tcpRtoMax":"120000",
             "tcpMaxConn":"4294967295",
             "tcpActiveOpens":"15898",
             "tcpPassiveOpens":"15868",
             "tcpAttemptFails":"33",
             "tcpEstabResets":"6",
             "tcpCurrEstab":"139",
             "tcpInSegs":"2353543",
             "tcpOutSegs":"2352093",
             "tcpRetransSegs":"76",
             "tcpInErrs":"0",
             "tcpOutRsts":"247",
             "tcpInCsumErrors":"0"
            },
            {
             "counterBlock_tag":"0:2008",
             "icmpInMsgs":"9630",
             "icmpInErrors":"781",
             "icmpInDestUnreachs":"0",
             "icmpInTimeExcds":"8458",
             "icmpInParamProbs":"0",
             "icmpInSrcQuenchs":"0",
             "icmpInRedirects":"0",
             "icmpInEchos":"0",
             "icmpInEchoReps":"1172",
             "icmpInTimestamps":"0",
             "icmpInAddrMasks":"0",
             "icmpInAddrMaskReps":"0",
             "icmpOutMsgs":"0",
             "icmpOutErrors":"0",
             "icmpOutDestUnreachs":"478",
             "icmpOutTimeExcds":"0",
             "icmpOutParamProbs":"86",
             "icmpOutSrcQuenchs":"0",
             "icmpOutRedirects":"0",
             "icmpOutEchos":"0",
             "icmpOutEchoReps":"0",
             "icmpOutTimestamps":"0",
             "icmpOutTimestampReps":"392",
             "icmpOutAddrMasks":"0",
             "icmpOutAddrMaskReps":"0"
            },
            {
             "counterBlock_tag":"0:2007",
             "ipForwarding":"1",
             "ipDefaultTTL":"64",
             "ipInReceives":"2438779",
             "ipInHdrErrors":"0",
             "ipInAddrErrors":"0",
             "ipForwDatagrams":"0",
             "ipInUnknownProtos":"0",
             "ipInDiscards":"0",
             "ipInDelivers":"2438779",
             "ipOutRequests":"2436226",
             "ipOutDiscards":"0",
             "ipOutNoRoutes":"29",
             "ipReasmTimeout":"0",
             "ipReasmReqds":"0",
             "ipReasmOKs":"0",
             "ipReasmFails":"0",
             "ipFragOKs":"0",
             "ipFragFails":"0",
             "ipFragCreates":"0"
            },
            {
             "counterBlock_tag":"0:2005",
             "disk_total":"29229203456",
             "disk_free":"24464830464",
             "disk_partition_max_used":"16.30",
             "disk_reads":"68752",
             "disk_bytes_read":"1542270464",
             "disk_read_time":"10224",
             "disk_writes":"50990",
             "disk_bytes_written":"552132608",
             "disk_write_time":"32023"
            },
            {
             "counterBlock_tag":"0:2004",
             "mem_total":"8233574400",
             "mem_free":"2374971392",
             "mem_shared":"0",
             "mem_buffers":"77352960",
             "mem_cached":"1550581760",
             "swap_total":"0",
             "swap_free":"0",
             "page_in":"909143",
             "page_out":"312564",
             "swap_in":"0",
             "swap_out":"0"
            },
            {
             "counterBlock_tag":"0:2003",
             "cpu_load_one":"0.510",
             "cpu_load_five":"0.620",
             "cpu_load_fifteen":"0.770",
             "cpu_proc_run":"0",
             "cpu_proc_total":"720",
             "cpu_num":"8",
             "cpu_speed":"798",
             "cpu_uptime":"8453",
             "cpu_user":"3732040",
             "cpu_nice":"60",
             "cpu_system":"2035020",
             "cpu_idle":"61113860",
             "cpu_wio":"7070",
             "cpuintr":"0",
             "cpu_sintr":"280770",
             "cpuinterrupts":"113556861",
             "cpu_contexts":"189573239",
             "cpu_steal":"0",
             "cpu_guest":"0",
             "cpu_guest_nice":"0"
            },
            {
             "counterBlock_tag":"0:2006",
             "nio_bytes_in":"10295941",
             "nio_pkts_in":"57332",
             "nio_errs_in":"0",
             "nio_drops_in":"0",
             "nio_bytes_out":"22354959",
             "nio_pkts_out":"0",
             "nio_errs_out":"0",
             "nio_drops_out":"0"
            },
            {
             "counterBlock_tag":"0:2000",
             "hostname":"r-tigris-25",
             "UUID":"8a704d6c-94b4-11ec-8000-900a8412a000",
             "machine_type":"3",
             "os_name":"2",
             "os_release":"5.10.0-8-2-amd64"
            }
           ]
          }
         ]
        }
    :param sample_file: sflowtool json format file
    :param interface_name: the specific interface name
    :param loopback: True if the traffic is sent via loopback ports
    :return: time stamp list for a specific interface and flow sample count number
    """
    time_stamps = {}
    time_stamps[interface_name] = []
    flow_sample_count = 0
    with open(sample_file, 'r') as sflow_data:
        logger.info(f"Loading {sample_file} ...")
        datagrams = json.load(sflow_data)
        logger.info(f"Parsing {sample_file} ...")
        for datagram in datagrams:
            localtime = datagram["localtime"]
            samples = datagram["samples"]
            for sample in samples:
                sample_type = sample["sampleType"]
                update_time_stamp_list(sample, sample_type, interface_name, time_stamps, localtime)
                if sample_type == "FLOWSAMPLE":
                    for element in sample["elements"]:
                        flow_sample_count += update_flow_sample_count(element, sample_type, loopback=loopback)
    sflow_data.close()
    return time_stamps[interface_name], flow_sample_count


def update_time_stamp_list(sample, sample_type, interface_name, time_stamps, localtime):
    """
    This method is used to update time stamp list
    :param sample: sample in samples list
    :param sample_type: sample type, FLOWSAMPLE or "COUNTERSSAMPLE"
    :param interface_name: interface name
    :param time_stamps: time_stamp list
    :param localtime: time stamp in sample
    """
    if interface_name is None:
        return
    if sample_type == "COUNTERSSAMPLE":
        for element in sample["elements"]:
            if element.get('ifName') == interface_name:
                logger.info(f"One {sample_type} for {interface_name} found - time stamp is {localtime}")
                time_stamps[interface_name].append(localtime)


def update_flow_sample_count(element, sample_type, loopback=False):
    """
    This method is used to check whether it's a valid flow sample
    :param element:
    :param sample_type:
    :param loopback: True if the traffic is sent via loopback ports
    :return: valid flow sample number
    """
    expected_packet = {"srcIP": None, "dstIP": None}
    actual_src_ip = element.get("srcIP")
    actual_dst_ip = element.get("dstIP")
    if not loopback:
        expected_packet["srcIP"] = SflowConsts.HA_DUT_1_IP
        expected_packet["dstIP"] = SflowConsts.HA_DUT_2_IP
    else:
        expected_packet["srcIP"] = SflowConsts.DUMMY_SOURCE_LOOPBACK_IP
        expected_packet["dstIP"] = SflowConsts.LOOPBACK_DEST_PORT_IP

    if actual_src_ip == expected_packet["srcIP"] and actual_dst_ip == expected_packet["dstIP"]:
        logger.info("One {} found - srcIP: {}, dstIP: {} srcMac: {} dstMAC: {}"
                    .format(sample_type, actual_src_ip, actual_dst_ip, element['srcMAC'], element['dstMAC']))
        return 1
    return 0


def format_json(engines, sample_file):
    """
    This method is used to format raw json file
    It substitutes blank with comma, and add "[" to the first line, then add "]" to the last line
    :param sample_file: sample json file
    """
    hb_engine = engines.hb
    logger.info(f"Formatting {sample_file} to normal JSON format")
    hb_engine.run_cmd(f"sed -i 's/^$/,/g' {sample_file}")
    hb_engine.run_cmd(f"sed -i '1i[' {sample_file}")
    hb_engine.run_cmd(f"echo ']' >> {sample_file}")


def analyze_time_stamp(time_stamp_list, polling_interval, sample_file):
    """
    This method is used to analyze time stamp
    The typical time step in sflow sample is listed
    "localtime":"2022-06-16T13:35:49+0000"
    Use "localtime" as time stamp
    :param time_stamp_list: time stamp list for a specific interface
    :param polling_interval: sflow polling interval
    :param sample_file: temp json file
    """
    formatted_time_stamp = []
    logger.info(f"Analyzing timestamps parsed from {sample_file}")
    logger.info(f"##-------------------------------##")
    for t in time_stamp_list:
        # remove '+0000' from time stamp
        t = t[:-5]
        new_t = datetime.strptime(t, "%Y-%m-%dT%H:%M:%S")
        formatted_time_stamp.append(new_t)
        logger.info(f"Timestamp: {new_t}")
    logger.info(f"##-------------------------------##")
    logger.info(f"polling_interval = {polling_interval}")
    max_deviation = 0
    for index in range(1, min(3, len(formatted_time_stamp))):
        delta = (formatted_time_stamp[index] - formatted_time_stamp[index - 1]).seconds
        logger.info(f"delta_t{index} = {delta}")
        deviation = abs(delta - polling_interval)
        max_deviation = max(max_deviation, deviation)
    logger.info(f"##-------------------------------##")
    if max_deviation > SflowConsts.POLLING_INTERVAL_DEVIATION_TOLERANCE:
        logger.warning(
            f"There is a big deviation: {max_deviation} seconds, greater than {SflowConsts.POLLING_INTERVAL_DEVIATION_TOLERANCE}")


def copy_sample_file_to_ngts_docker(engines, sample_file):
    """
    This method is used to copy temp sample json file from host B to ngts docker
    :param engines: engines fixture
    :param sample_file: temp sample json file
    """
    hb_engine = engines.hb
    logger.info(f"Start copying {sample_file} from hb to ngts docker")
    hb_engine.copy_file(source_file=sample_file, dest_file=sample_file, file_system='/', direction="get",
                        overwrite_file=True, verify_file=False)


def get_random_port(topology_obj):
    """
    This method is used to get one port randomly from dut_ha_1/dut_ha_1/dut_hb_1/dut_hb_2
    :param topology_obj: get_random_port fixture
    :return: port name
    """
    dut_ha_1 = topology_obj.ports.get('dut-ha-1')
    dut_ha_2 = topology_obj.ports.get('dut-ha-2')
    dut_hb_1 = topology_obj.ports.get('dut-hb-1')
    dut_hb_2 = topology_obj.ports.get('dut-hb-2')
    random_port = random.choice([dut_ha_1, dut_ha_2, dut_hb_1, dut_hb_2])
    logger.info(f"Randomly choose one port: {random_port} to validate polling interval")
    return random_port


@retry(Exception, tries=5, delay=5)
def verify_sflow_sample_polling_interval(engines, topology_obj, collector, polling_interval):
    """
    This method is used to verify polling interval for a specific interface
    The interface is chosen from all the interfaces list randomly
    :param engines: engines fixture
    :param topology_obj: topology_obj fixture
    :param collector: collector name
    :param polling_interval: polling interval value
    """
    hb_engine = engines.hb
    count = 0
    random_port = get_random_port(topology_obj)
    sflowtool_pid = start_sflowtool_process(engines, collector)
    try:
        if polling_interval == 0:
            logger.info(f"Collect {SflowConsts.POLLING_INTF_0_WAIT_TIME} seconds when polling interval is set to 0")
            time.sleep(SflowConsts.POLLING_INTF_0_WAIT_TIME)
            result = hb_engine.run_cmd(
                f"grep -ia '\"{random_port}\"' {SflowConsts.COLLECTOR[collector]['sample_file']} | wc -l")
            assert int(result) == 0, f"Sflow samples are received at polling interval 0"
            return
        # polling_interval * 5 is for reboot test, polling_interval * 4 is enough for normal test case
        while count < polling_interval * 5:
            result = hb_engine.run_cmd(
                f"grep -ia '\"{random_port}\"' {SflowConsts.COLLECTOR[collector]['sample_file']} | wc -l")
            # 3 captured samples are enough for calculate time stamp
            if int(result) >= 3:
                break
            time.sleep(1)
            count += 1
    finally:
        kill_sflowtool_process(engines, sflowtool_pid)
    format_json(engines, SflowConsts.COLLECTOR[collector]['sample_file'])
    copy_sample_file_to_ngts_docker(engines, SflowConsts.COLLECTOR[collector]['sample_file'])
    time_stamp_list, _ = analyze_sample(SflowConsts.COLLECTOR[collector]['sample_file'], random_port)
    analyze_time_stamp(time_stamp_list, polling_interval, SflowConsts.COLLECTOR[collector]['sample_file'])


def send_traffic(engines, interfaces, topology_obj, source_iface_mac, dest_iface_mac, loopback=False,
                 src_iface_name=None):
    """
    This method is used to send traffic
    :param interfaces: interface fixture
    :param topology_obj: topology_obj fixture
    :param source_iface_mac: mac address of source interface
    :param dest_iface_mac: mac address of destination interface
    :param loopback: True if the traffic is sent via loopback ports
    :param src_iface_name: name of the source interface
    """
    logger.info(f"Start sending traffic ...")
    packet_length = random.randint(100, 1000)
    src_ip = SflowConsts.HA_DUT_1_IP
    dest_ip = SflowConsts.HA_DUT_2_IP
    if loopback:
        src_ip = SflowConsts.DUMMY_SOURCE_LOOPBACK_IP
        dest_ip = SflowConsts.LOOPBACK_DEST_PORT_IP

    pkt_udp = f'Ether(dst="{dest_iface_mac}", src="{source_iface_mac}")/IP(src="{src_ip}", dst="{dest_ip}", len={packet_length})/UDP()'
    pkt_tcp = f'Ether(dst="{dest_iface_mac}", src="{source_iface_mac}")/IP(src="{src_ip}", dst="{dest_ip}", len={packet_length})/TCP()'
    pkt_icmp = f'Ether(dst="{dest_iface_mac}", src="{source_iface_mac}")/IP(src="{src_ip}", dst="{dest_ip}", len={packet_length})/ICMP()'
    pkt_ip = f'Ether(dst="{dest_iface_mac}", src="{source_iface_mac}")/IP(src="{src_ip}", dst="{dest_ip}", len={packet_length})'

    pkt_list = [pkt_udp, pkt_tcp, pkt_icmp, pkt_ip]

    random_pkt = random.choice(pkt_list)
    logger.info(f"Randomly choose {random_pkt} from udp/tcp/ip/icmp packet type")
    logger.info(f"Sending {SflowConsts.SEND_PACKET_NUM} {random_pkt} ...")
    if not loopback:
        #  send packets via ScapyChecker class
        validation_r = {'sender': 'ha',
                        'send_args': {'interface': f'{interfaces.ha_dut_1}',
                                      'packets': random_pkt, 'count': SflowConsts.SEND_PACKET_NUM}}
        scapy_sender = ScapyChecker(topology_obj.players, validation_r)
        scapy_sender.run_validation()
    else:
        #  send packets via scapy python script copied to the switch
        handle_and_send_scapy_python_script(engines, random_pkt, src_iface_name)


def handle_and_send_scapy_python_script(engines, random_pkt, src_iface_name):
    """
    This method is used to create python file that uses scapy to send traffic from the switch,
    The file is created from template with a random packet that's being added to it, copied to the switch and runs the traffic.
    :param engines: engines fixture
    :param random_pkt: packet that was chosen from a list of packet types
    :param src_iface_name: name of the source interface
    """
    try:
        scapy_template_file = "scapy_send_template.py"
        scapy_file_tmp_dir = os.path.join('/tmp/', scapy_template_file)
        os.system('cp {} {}'.format(SCAPY_TEMPLATE_DIR, scapy_file_tmp_dir))
        with open(scapy_file_tmp_dir, 'a') as send_script:
            send_script.write('pkt = {}\n'.format(random_pkt))
            send_script.write('\nsend_packet(pkt, {}, "{}")\n'.format(SflowConsts.SEND_PACKET_NUM, src_iface_name))
        engines.dut.copy_file(source_file=scapy_file_tmp_dir, dest_file='send_scapy.py',
                              file_system='/home/admin', overwrite_file=True, verify_file=False)
        engines.dut.run_cmd('sudo python3 send_scapy.py', validate=True)

    except Exception as err:
        raise AssertionError(err)

    finally:
        os.system('rm -rf {}'.format(scapy_file_tmp_dir))
        engines.dut.run_cmd('sudo rm -rf send_scapy.py')


@retry(Exception, tries=5, delay=5)
def verify_flow_sample_received(engines, interfaces, topology_obj, collector, sample_rate, source_iface_mac,
                                dest_iface_mac, sample_exist=True, loopback=False, src_iface_name=None):
    """
    This method is used to verify flow sample in the sample file
    :param engines: engines fixture
    :param interfaces: interfaces fixture
    :param topology_obj: topology_obj fixture
    :param collector: collector name
    :param sample_rate: sample rate
    :param source_iface_mac: mac address of source interface
    :param dest_iface_mac: mac address of destination interface
    :param sample_exist: suppose flow sample exist or not
    :param loopback: True if the traffic is sent via loopback ports
    :param src_iface_name: name of the source interface

    """
    hb_engine = engines.hb

    sflowtool_pid = start_sflowtool_process(engines, collector)
    send_traffic(engines, interfaces, topology_obj, source_iface_mac, dest_iface_mac, loopback=loopback,
                 src_iface_name=src_iface_name)

    # 500 ms is for three main processes
    # 1. Sflow docker collect, process and send  sflow samples
    # 2. Sflowtool process the received sflow samples
    # 3. Sflowtool format all the sflow samples into json format and write them into json file
    time.sleep(0.5)
    kill_sflowtool_process(engines, sflowtool_pid)

    if sample_exist:
        format_json(engines, SflowConsts.COLLECTOR[collector]['sample_file'])
        copy_sample_file_to_ngts_docker(engines, SflowConsts.COLLECTOR[collector]['sample_file'])

        logger.info("Start analyzing flow samples")
        _, flow_sample_count = analyze_sample(SflowConsts.COLLECTOR[collector]['sample_file'], loopback=loopback)
        count = SflowConsts.SEND_PACKET_NUM // sample_rate
        logger.info("##-------------------------------##")
        logger.info(
            f"There should be at least {count}, at most {count + 1} flow samples due to the sflow random sample algorithm")
        logger.info(f"{flow_sample_count} flow sample received in {SflowConsts.COLLECTOR[collector]['sample_file']}")
        logger.info("##-------------------------------##")

        assert count <= flow_sample_count <= count + 1, f"Received {flow_sample_count} flow samples which is not in correct range!"
    else:
        logger.info("Start analyzing flow samples")
        flow_sample_count = hb_engine.run_cmd(
            f"grep FLOWSAMPLE {SflowConsts.COLLECTOR[collector]['sample_file']} | wc -l")
        logger.info("##-------------------------------##")
        logger.info(f"There should be 0 flow samples")
        logger.info(f"{flow_sample_count} flow sample received in {SflowConsts.COLLECTOR[collector]['sample_file']}")
        logger.info("##-------------------------------##")
        assert int(flow_sample_count) == 0, f"Received {flow_sample_count} flow samples which should be 0"
