# Setups where BMC or management does not support IPv6; skip IPv6-only tests on these.
SETUPS_WITHOUT_IPV6_BMC = (
    'NVOS_rosalind_eb1_10', 'NVOS_rosalind_nvos_2182', 'NVOS_rosalind_nvos_2164'
)


class Transceiver:
    def __init__(self, transceiver_type, last_release_path, test_versions_path,
                 upgrade_version_name, downgrade_version_name,
                 upgrade_version_number, downgrade_version_number,
                 installation_time):
        self.transceiver_type = transceiver_type
        self.last_release_path = last_release_path
        self.test_versions_path = test_versions_path
        self.upgrade_version_name = upgrade_version_name
        self.downgrade_version_name = downgrade_version_name
        self.upgrade_version_number = upgrade_version_number
        self.downgrade_version_number = downgrade_version_number
        self.installation_time = installation_time

    def update_versions(self):
        self.upgrade_version_name = self.upgrade_version_name.replace('dev', 'pk')
        self.downgrade_version_name = self.downgrade_version_name.replace('dev', 'pk')


class TransceiversConsts:
    TRANSCEIVER_TYPE = 'type'
    TRANSCEIVER_LAST_RELEASE_PATH = "releases_path"
    TRANSCEIVER_TEST_VERSIONS_PATH = "test_versions_path"
    TRANSCEIVER_UPGRADE_VERSION_NAME = 'upgrade_versions'
    TRANSCEIVER_DOWNGRADE_VERSION_NAME = 'downgrade_versions'
    TRANSCEIVER_UPGRADE_VERSION_NUMBER = 'upgrade_fw'
    TRANSCEIVER_DOWNGRADE_VERSION_NUMBER = 'downgrade_fw'
    TRANSCEIVER_INSTALLATION_TIME = "installation_time"
    TRANSCEIVERS_APPROVED_FIRMWARES_PATH = '/auto/sw_system_project/NVOS_INFRA/verification_files/transceiver_fw/'
    TRANSCEIVERS_FIRMWARES_PATH = '/.autodirect/sw/release/fwshared/linkx/mlnx_linkx_module_aoc_fw/'
    TRANSCEIVERS_RELEASE = '230_00_release'     # the latest approved release
    TRANSCEIVERS_VENDOR_SN = 'vendor-sn'
    TRANSCEIVERS_ELS = 'els'
    TRANSCEIVERS_OE = 'oe'
    TRANSCEIVERS_SW = 'sw'
    TRANSCEIVERS_FIELDS = {
        TRANSCEIVERS_ELS: ['channel', 'diagnostics-status', 'els-initialization', 'els-oper-state', 'error-status',
                           'fault-condition', 'fw-version', 'identifier', 'oe-mapping', 'port-mapping', 'status',
                           'temperature', 'vendor-date-code', 'vendor-name', 'vendor-pn', 'vendor-rev', 'vendor-sn',
                           'voltage'],
        TRANSCEIVERS_OE: ['channel', 'diagnostics-status', 'dp-fw-fault', 'els-mapping', 'error-status', 'fw-version',
                          'identifier', 'mod-fw-fault', 'port-mapping', 'status', 'temperature', 'vendor-date-code',
                          'vendor-name', 'vendor-pn', 'voltage'],
        TRANSCEIVERS_SW: ['cable-length', 'cable-type', 'diagnostics-status', 'dp-fw-fault', 'error-status',
                          'fw-version', 'identifier', 'mod-fw-fault', 'status', 'vendor-date-code', 'vendor-name',
                          'vendor-pn', 'vendor-rev', 'vendor-sn']
    }
    # Expected sub-dict fields for ELS/OE nested structures
    ELS_TEMPERATURE_FIELDS = ['temperature', 'high-alarm-threshold']
    ELS_VOLTAGE_FIELDS = ['voltage', 'high-alarm-threshold', 'low-alarm-threshold']
    ELS_INIT_LASER_FIELDS = ['fiber-check', 'laser-tuning', 'laser-up']
    ELS_CHANNEL_FIELDS = ['laser-setpoint', 'laser-power', 'tec-temp',
                          'rx-cdr-lol', 'rx-los', 'tx-ad-eq-fault', 'tx-cdr-lol', 'tx-los', 'tx-fault']

    ELS_TEC_TEMP_MIN = 38.0
    ELS_TEC_TEMP_MAX = 42.0

    OE_TEMPERATURE_FIELDS = ['temperature', 'high-alarm-threshold', 'low-alarm-threshold']
    OE_VOLTAGE_FIELDS = ['voltage', 'high-alarm-threshold', 'low-alarm-threshold']
    OE_CHANNEL_FIELDS = ['rx-power', 'tx-power', 'oe-lane-temperature', 'els-input-power',
                         'rx-cdr-lol', 'rx-los', 'tx-ad-eq-fault', 'tx-cdr-lol', 'tx-los', 'tx-fault']
    OE_RX_POWER_FIELDS = ['power', 'high-alarm-threshold', 'low-alarm-threshold']
    OE_TX_POWER_FIELDS = ['power', 'high-alarm-threshold', 'low-alarm-threshold']

    TRANSCEIVERS_DETAILS = {  # TODO [L.A] Add Taipan transceiver when known
        '39': Transceiver(
            transceiver_type='Xodin',
            last_release_path=f"{TRANSCEIVERS_FIRMWARES_PATH}39_Xodin/{TRANSCEIVERS_RELEASE}",
            test_versions_path=f"{TRANSCEIVERS_APPROVED_FIRMWARES_PATH}Xodin/",
            upgrade_version_name="fw_39_230_00032_dev_signed.bin",
            downgrade_version_name="fw_39_230_00030_dev_signed.bin",
            upgrade_version_number="39.230.32",
            downgrade_version_number="39.230.30",
            installation_time=180
        ),
        '70': Transceiver(
            transceiver_type='Wolverine',
            last_release_path=f"{TRANSCEIVERS_FIRMWARES_PATH}70_Wolverine/{TRANSCEIVERS_RELEASE}",
            test_versions_path=f"{TRANSCEIVERS_APPROVED_FIRMWARES_PATH}Wolverine/",
            upgrade_version_name="fw_70_250_00097_dev_signed_WOLVERINE_DK.bin",
            downgrade_version_name="fw_70_250_00096_dev_signed_WOLVERINE_DK.bin",
            upgrade_version_number="70.250.97",
            downgrade_version_number="70.250.96",
            installation_time=300
        ),
        '110': Transceiver(
            transceiver_type='Wolverine_sian3',
            last_release_path=f"{TRANSCEIVERS_FIRMWARES_PATH}110_Wolverine2_s3/{TRANSCEIVERS_RELEASE}",
            test_versions_path=f"{TRANSCEIVERS_APPROVED_FIRMWARES_PATH}Wolverine_sian3/",
            upgrade_version_name="fw_110_030_00102_dev_signed.bin",
            downgrade_version_name="fw_110_030_00086_dev_signed.bin",
            upgrade_version_number="110.30.102",
            downgrade_version_number="110.30.86",
            installation_time=350
        ),
        '130': Transceiver(
            transceiver_type='Sian2',
            last_release_path=f"TODO",
            test_versions_path=f"{TRANSCEIVERS_APPROVED_FIRMWARES_PATH}Sian2/",
            upgrade_version_name="fw_130_245_0000_dev_signed_SIAN2_DK.bin",
            downgrade_version_name="fw_130_245_0000_dev_signed_SIAN2_DK.bin",
            upgrade_version_number="130.245.0",
            downgrade_version_number="130.245.0",
            installation_time=480
        ),
        '134': Transceiver(
            transceiver_type='Gorgon',
            last_release_path=f"TODO",
            test_versions_path=f"{TRANSCEIVERS_APPROVED_FIRMWARES_PATH}Gorgon/",
            upgrade_version_name="Gorgon_134.7_innolight_wr.bin",
            downgrade_version_name="Gorgon_134.5_innolight_wr.bin",
            upgrade_version_number="134.7.0",
            downgrade_version_number="134.5.0",
            installation_time=300
        ),
        '46': Transceiver(
            transceiver_type='Bagheera',
            last_release_path=f"{TRANSCEIVERS_FIRMWARES_PATH}46_Bagheera1_2/{TRANSCEIVERS_RELEASE}",
            test_versions_path=f"{TRANSCEIVERS_APPROVED_FIRMWARES_PATH}Bagheera/",
            upgrade_version_name="fw_46_230_00018_dev_signed.bin",
            downgrade_version_name="fw_46_230_00014_dev_signed.bin",
            upgrade_version_number="46.230.18",
            downgrade_version_number="46.230.14",
            installation_time=270
        ),
        '47': Transceiver(
            transceiver_type='Louie',
            last_release_path=f"{TRANSCEIVERS_FIRMWARES_PATH}47_Louie1_4/{TRANSCEIVERS_RELEASE}",
            test_versions_path=f"{TRANSCEIVERS_APPROVED_FIRMWARES_PATH}Louie/",
            upgrade_version_name="fw_47_230_01018_dev_signed.bin",
            downgrade_version_name="fw_47_230_01014_dev_signed.bin",
            upgrade_version_number="47.230.18",
            downgrade_version_number="47.230.14",
            installation_time=60
        ),
        '44': Transceiver(
            transceiver_type='Wolverine2-Arcus2',
            last_release_path=f"{TRANSCEIVERS_FIRMWARES_PATH}44_Wolverine2_arcus2/{TRANSCEIVERS_RELEASE}",
            test_versions_path="/auto/sw_system_project/NVOS_INFRA/verification_files/module_fw/Wolverine2_arcus2/",
            upgrade_version_name="fw_44_040_02035_dev_signed_WOLVERINE2-ARCUS2_DK.bin",
            downgrade_version_name="fw_44_040_02030_dev_signed_WOLVERINE2-ARCUS2_DK.bin",
            upgrade_version_number="44.40.2035",
            downgrade_version_number="44.40.2030",
            installation_time=285
        )
    }
