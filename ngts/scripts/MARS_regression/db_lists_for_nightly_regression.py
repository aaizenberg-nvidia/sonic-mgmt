MARS_DBS_PATH = 'sonic-mgmt/sonic-tool/mars/dbs/'
PLATFORM_AGNOSTIC_GROUP1 = 'platform_agnostic_group_1'
PLATFORM_AGNOSTIC_GROUP2 = 'platform_agnostic_group_2'
SETUPS_GROUP_1 = 'setups_group_1'
SETUPS_GROUP_2 = 'setups_group_2'
PLATFORM_DEPENDENT = 'platform_dependent'


COMMUNITY_SET1_TEST_GROUP_MAP = {
    'community/pretest.db': PLATFORM_DEPENDENT,
    'community/routes.db': PLATFORM_AGNOSTIC_GROUP1,
    'community/counters.db': PLATFORM_DEPENDENT,
    'community/dhcp.db': PLATFORM_AGNOSTIC_GROUP2,
    'community/pfcwd.db': PLATFORM_AGNOSTIC_GROUP2,
    'community/layer2.db': PLATFORM_AGNOSTIC_GROUP1,
    'community/layer3.db': PLATFORM_AGNOSTIC_GROUP1,
    'community/memory.db': PLATFORM_DEPENDENT,
    'community/common.db': PLATFORM_DEPENDENT,
    'community/snmp.db': PLATFORM_AGNOSTIC_GROUP1,
    'community/wjh.db': PLATFORM_AGNOSTIC_GROUP2,
    'community/srv6.db': PLATFORM_DEPENDENT,
    'community/packet_trimming.db': PLATFORM_DEPENDENT,
    'community/high_frequency_telemetry.db': PLATFORM_DEPENDENT,
    'community/warm_reboot.db': PLATFORM_DEPENDENT,
    'community/span.db': PLATFORM_DEPENDENT,
    'community/fast_reboot.db': PLATFORM_DEPENDENT,
    'community/mgmtvrf.db': PLATFORM_DEPENDENT,
    'community/pre_rpc.db': PLATFORM_DEPENDENT,
    'community/rpc_qos.db': PLATFORM_DEPENDENT,
    'community/rpc_pfc_asym_and_copp.db': PLATFORM_DEPENDENT,
    'community/rpc_qos_dualtor.db': PLATFORM_DEPENDENT,
    'community/post_rpc.db': PLATFORM_DEPENDENT,
    'community/system.db': PLATFORM_DEPENDENT,
    'community/bsl.db': PLATFORM_DEPENDENT,
    'community/smartswitch.db': PLATFORM_DEPENDENT,
    'community/dash_smartswitch.db': PLATFORM_DEPENDENT,
    'community/platform_dpu_on_ss.db': PLATFORM_DEPENDENT,
    'community/techsupport_dpu_on_ss.db': PLATFORM_DEPENDENT,
}


COMMUNITY_SET2_TEST_GROUP_MAP = {
    'community/pretest.db': PLATFORM_DEPENDENT,
    'community/acl.db': PLATFORM_AGNOSTIC_GROUP2,
    'community/bfd.db': PLATFORM_DEPENDENT,
    'community/radv.db': PLATFORM_DEPENDENT,
    'community/ecmp.db': PLATFORM_DEPENDENT,
    'community/ip_neigh.db': PLATFORM_AGNOSTIC_GROUP1,
    'community/resources.db': PLATFORM_AGNOSTIC_GROUP1,
    'community/tunnel.db': PLATFORM_AGNOSTIC_GROUP1,
    'community/generic_hash.db': PLATFORM_DEPENDENT,
    'community/techsupport.db': PLATFORM_DEPENDENT,
    'community/interfaces.db': PLATFORM_AGNOSTIC_GROUP1,
    'community/sub_port_interfaces.db': PLATFORM_DEPENDENT,
    'community/system_health.db': PLATFORM_DEPENDENT,
    'community/sflow.db': PLATFORM_AGNOSTIC_GROUP2,
    'community/bgp.db': PLATFORM_AGNOSTIC_GROUP2,
    'community/pbh.db': PLATFORM_DEPENDENT,
    'community/generic_config_updater.db': PLATFORM_AGNOSTIC_GROUP2,
    'community/autorestart.db': PLATFORM_DEPENDENT,
    'community/dualtor.db': PLATFORM_DEPENDENT,
    'community/memory.db': PLATFORM_DEPENDENT,
    'community/link.db': PLATFORM_AGNOSTIC_GROUP1,
    'community/macsec.db': PLATFORM_DEPENDENT,
    'community/voq.db': PLATFORM_DEPENDENT,
    'community/upgrade_related.db': PLATFORM_DEPENDENT,
    'community/sed_password.db': PLATFORM_DEPENDENT
}


CANONICAL_TEST_GROUP_MAP = {
    'canonical/pretest.db': PLATFORM_DEPENDENT,
    'community/dynamic_buffer.db': PLATFORM_AGNOSTIC_GROUP2,
    'community/platform.db': PLATFORM_DEPENDENT,
    'community/clock.db': PLATFORM_DEPENDENT,
    'community/techsupport_any_topo.db': PLATFORM_DEPENDENT,
    'canonical/nightly.db': PLATFORM_AGNOSTIC_GROUP1,
    'canonical/push_gate_with_upgrade.db': PLATFORM_DEPENDENT,
    'community/platform_fwutil.db': PLATFORM_DEPENDENT,
    'canonical/secure_boot.db': PLATFORM_DEPENDENT,
    'community/ipv6_mgmt.db': PLATFORM_DEPENDENT
}

TEST_GROUP_MAP = {
    'community_set1': COMMUNITY_SET1_TEST_GROUP_MAP,
    'community_set2': COMMUNITY_SET2_TEST_GROUP_MAP,
    'canonical': CANONICAL_TEST_GROUP_MAP
}

CONTROL_PLANE_TESTS_MAP = {
    'community': {'setups': ['r-leopard-01_setup', 'mtvr-leopard-01_setup', 'r-panther-02_setup'], 'tests': ['community/mgmtvrf.db', 'community/system.db']},
    # community/ipv6_mgmt.db is covered by r-panther-02 setup. No need to run it on any canonical setups.
    'canonical': {'setups': [], 'tests': ['community/ipv6_mgmt.db']}
}


SETUPS_GROUPS_MAP = {
    'community_set1': {
        ('t0', 'spc1'): {'r-panther-02_setup': SETUPS_GROUP_1, 'arc-switch1004_setup': SETUPS_GROUP_2},
        ('t1', 'spc1'): {'arc-switch1025_setup': SETUPS_GROUP_1, 'r-panther-01_setup': SETUPS_GROUP_2},
        ('t0', 'spc3'): {'r-tigon-04_setup': SETUPS_GROUP_1, 'mtvr-leopard-01_setup': SETUPS_GROUP_2},
        ('t1', 'spc3'): {'r-leopard-01_setup': SETUPS_GROUP_1, 'r-tigon-11_setup': SETUPS_GROUP_2},
        ('t0', 'spc5'): {'r-bison-06_setup': SETUPS_GROUP_1, 'r-bison-08_setup': SETUPS_GROUP_2},
        ('t1', 'spc5'): {'r-bison-04_setup': SETUPS_GROUP_1},
    },
    'community_set2': {
        ('t0', 'spc1'): {'r-panther-42_setup': SETUPS_GROUP_1, 'r-panther-23_setup': SETUPS_GROUP_2},
        ('t1', 'spc1'): {'r-panther-40_setup': SETUPS_GROUP_1, 'r-panther-45_setup': SETUPS_GROUP_2},
        ('t0', 'spc3'): {'mtvr-leopard-09_setup': SETUPS_GROUP_1, 'r-tigon-21_setup': SETUPS_GROUP_2},
        ('t1', 'spc3'): {'r-leopard-58_setup': SETUPS_GROUP_1, 'r-tigon-20_setup': SETUPS_GROUP_2},
        ('t0', 'spc5'): {'r-bison-18_setup': SETUPS_GROUP_1, 'r-bison-16_setup': SETUPS_GROUP_2},
        ('t1', 'spc5'): {'r-bison-20_setup': SETUPS_GROUP_1, 'r-bison-22_setup': SETUPS_GROUP_2},
    },
    'canonical': {
        ('ptf_any', 'spc1'): {'sonic_panther_r-panther-03': SETUPS_GROUP_1, 'sonic_panther_r-panther-13': SETUPS_GROUP_2},
        ('ptf_any', 'spc2'): {'sonic_lionfish_r-lionfish-07': SETUPS_GROUP_1, 'sonic_lionfish_r-lionfish-13': SETUPS_GROUP_2},
        ('ptf_any', 'spc3'): {
            'sonic_tigon_r-tigon-15': SETUPS_GROUP_1,
            'sonic_tigon_r-tigon-17': SETUPS_GROUP_2,
            'sonic_leopard_r-leopard-41': SETUPS_GROUP_1,
            'sonic_leopard_r-leopard-56': SETUPS_GROUP_2,
            'sonic_leopard_r-leopard-32': SETUPS_GROUP_2,
            'sonic_bobcat_bobcat-10': SETUPS_GROUP_1
        },
        ('ptf_any', 'spc4'): {'sonic_moose_mtvr-moose-11': SETUPS_GROUP_1,
                              'sonic_moose_mtvr-moose-09': SETUPS_GROUP_1,
                              'sonic_moose_r-moose-02': SETUPS_GROUP_2},
        ('ptf_any', 'spc5'): {'sonic_bison_mtvr-bison-133': SETUPS_GROUP_1,
                              'sonic_bison_r-bison-11': SETUPS_GROUP_1,
                              'sonic_bison_r-bison-12': SETUPS_GROUP_2},
        ('ptf_any', 'spc6'): {'sonic_slm-60': SETUPS_GROUP_1,
                              'sonic_slm-110': SETUPS_GROUP_1,
                              'sonic_slm-111': SETUPS_GROUP_2}
    }
}
# In canonical we have odd number of setups.

# --- Finding the other setups of the scenario, and the topo and the hwsku each of them runs ---

# Name of the environment variable Mars sets to the path of the scenario json of the current run.
SCENARIO_JSON_ENV_VAR = 'JSON_FILE'

# Key of a setup in that json, holding the command Mars runs to compute the dbs of that setup. It is
# a generic key, a setup without it or with a command running another script takes its dbs from
# somewhere else, see read_scenario_setups().
SCENARIO_CALCULATE_PARAMS_KEY = 'MARS_ORCH_CALCULATE_PARAMS_COMMAND'

# Options of extra_args_inst in that json, a setup declaring them overrides its .setup file.
SCENARIO_TOPOLOGY_OPTION = '--meinfo_topology'
SCENARIO_HWSKU_OPTION = '--meinfo_dut_hwsku'
SCENARIO_SETUP_CONF_OPTION = '--conf'

# The .setup file of a setup, holding its default topo and hwsku as xml like tags.
SETUP_CONF_DIR = '/auto/sw_regression/system/SONIC/MARS/conf/setups'
SETUP_CONF_TOPOLOGY_TAG = 'topology'
SETUP_CONF_HWSKU_TAG = 'dut_hwsku'

# Both files above live on the /auto mounts, which fail sporadically. A single failed read would
# silently disable the sharing, so reading them is retried.
NFS_READ_RETRIES = 3
NFS_RETRY_DELAY_SECONDS = 1


# --- Dbs that stay out of the sharing between the setups with the same topo and hwsku ---

# Run by every one of those setups instead of being given to a single one.
EVERY_SETUP_DBS = ['community/pretest.db']

# Dbs depending on each other and on their order, they are given to a single setup as one block.
INDIVISIBLE_DB_BLOCKS = [
    ['community/pre_rpc.db',
     'community/rpc_qos.db',
     'community/rpc_pfc_asym_and_copp.db',
     'community/rpc_qos_dualtor.db',
     'community/post_rpc.db'],
]
