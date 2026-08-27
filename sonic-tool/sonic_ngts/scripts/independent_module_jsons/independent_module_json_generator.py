import argparse
import json
import os
import pandas as pd
from collections import OrderedDict


SPC3_SI_PARAMS_EXCEL = 'spc3_si_params.xls'
SPC4_SI_PARAMS_EXCEL = 'spc4_si_params.xls'
SPC6_SI_PARAMS_EXCEL = 'spc6_si_params.xls'

GLOBAL_MEDIA_SETTINGS = 'GLOBAL_MEDIA_SETTINGS'
SPC3_PORT_RANGE = '1-32'
SPC4_PORT_RANGE = '1-64'
DEFAULT = 'Default'
GROUP_ID = 0

SPC3 = 'spc3'
SPC4 = 'spc4'
SPC5 = 'spc5'
SPC6 = 'spc6'

PORT = 'port'       # media_settings.json
MODULE = 'module'   # optics_si_settings.json

NUM_OF_LANES = 8
LANE_PREFIX = 'lane'
SPEED_PREFIX = 'speed:'
MODULE_LANE_SPEED_SUFFIX = 'G_SPEED'

APPLICATION_ADVERTISEMENT = 'Application Advertisement'
IDENTIFIER = 'Identifier'
SPECIFICATION_COMPLIANCE = 'Specification compliance'
EEPROM_DATA_TO_EXTRACT = [APPLICATION_ADVERTISEMENT, IDENTIFIER, SPECIFICATION_COMPLIANCE]

ACTIVE = 'Active'

# Passive copper links are left on the SerDes defaults, so these are the only parameters set for them
PASSIVE_SI_PARAMS = {
    'tx_precoding': 1,
    'rx_precoding': 1
}

# The specification compliance reported for passive copper cables
PASSIVE_COPPER_SPEC_COMPLIANCE = 'passive_copper_media_interface'

SPEED = 'SPEED'
LANES = 'LANES'

LANE_SPEED_TO_GENERATION = {
    200: 'XDR',
    100: 'NDR',
    50: 'HDR',
    25: 'EDR',
    10: 'FDR',
    2.5: 'SDR',
    1: 'SGMII'
}

SPC3_EXCEL_SPEED_COLUMN_TO_SPEED_ALIAS = {
    'SDR': '10GbE',
    'FDR': 'FDR/56GbE',
    'EDR': 'EDR/100GbE',
    'HDR': 'HDR/200GbE'
}

SI_PARAMS_EXCEL = 'si_params_excel'
SI_PARAMS_SHEET = 'si_params_sheet'
SERDES_DEVICE = 'serdes_device'
SPEED_COLUMN = 'speed_column'
PORT_RANGE = 'port_range'
SPEED_ALIASES = 'speed_aliases'
PASSIVE_MIN_LANE_SPEED = 'passive_min_lane_speed'

# PASSIVE_MIN_LANE_SPEED of a generation whose passive copper SI settings are not maintained
PASSIVE_SKIP = None

# SI_PARAMS_EXCEL of a generation whose SI datasheet is not published yet. Only its passive copper settings,
# which are not taken from the datasheet, are generated - the remaining datasheet keys are left out of its config.
NO_SI_DATASHEET = None

SPC_CONFIG = {
    SPC3: {
        SI_PARAMS_EXCEL: SPC3_SI_PARAMS_EXCEL,
        SI_PARAMS_SHEET: 'TX_DB_HDR',
        SERDES_DEVICE: 'Firebird',
        SPEED_COLUMN: 'speed / protocol',
        PORT_RANGE: SPC3_PORT_RANGE,
        SPEED_ALIASES: SPC3_EXCEL_SPEED_COLUMN_TO_SPEED_ALIAS,
        PASSIVE_MIN_LANE_SPEED: PASSIVE_SKIP
    },
    SPC4: {
        SI_PARAMS_EXCEL: SPC4_SI_PARAMS_EXCEL,
        SI_PARAMS_SHEET: 'TX_DB',
        SERDES_DEVICE: 'Albatross',
        SPEED_COLUMN: 'speed / protocol',
        PORT_RANGE: SPC4_PORT_RANGE,
        SPEED_ALIASES: None,
        PASSIVE_MIN_LANE_SPEED: 50
    },
    SPC5: {
        SI_PARAMS_EXCEL: NO_SI_DATASHEET,
        PORT_RANGE: SPC4_PORT_RANGE,
        PASSIVE_MIN_LANE_SPEED: 50
    },
    SPC6: {
        SI_PARAMS_EXCEL: SPC6_SI_PARAMS_EXCEL,
        SI_PARAMS_SHEET: 'in',
        SERDES_DEVICE: 'Maglan',
        SPEED_COLUMN: 'Speed',
        PORT_RANGE: SPC4_PORT_RANGE,
        SPEED_ALIASES: None,
        PASSIVE_MIN_LANE_SPEED: 50
    }
}

APPLICATIONS_DATA_BY_SFF_SPEC = {
    # Ethernet
    '1000BASE-CX':      {SPEED: 1, LANES: 1},
    'XAUI':             {SPEED: 10, LANES: 4},
    'XFI':              {SPEED: 10, LANES: 1},
    'SFI':              {SPEED: 10, LANES: 1},
    '25GAUI':           {SPEED: 25, LANES: 1},
    'XLAUI':            {SPEED: 40, LANES: 4},
    'XLPPI':            {SPEED: 40, LANES: 4},
    'LAUI-2':           {SPEED: 50, LANES: 2},
    '50GAUI-2':         {SPEED: 50, LANES: 2},
    '50GAUI-1':         {SPEED: 50, LANES: 1},
    'CAUI-4':           {SPEED: 100, LANES: 4},
    '100GAUI-4':        {SPEED: 100, LANES: 4},
    '100GAUI-2':        {SPEED: 100, LANES: 2},
    '100GAUI-1-S':      {SPEED: 100, LANES: 1},
    '100GAUI-1-L':      {SPEED: 100, LANES: 1},
    '200GAUI-8':        {SPEED: 200, LANES: 8},
    '200GAUI-4':        {SPEED: 200, LANES: 4},
    '200GAUI-2-S':      {SPEED: 200, LANES: 2},
    '200GAUI-2-L':      {SPEED: 200, LANES: 2},
    '200GAUI-1':        {SPEED: 200, LANES: 1},
    '400GAUI-16':       {SPEED: 400, LANES: 16},
    '400GAUI-8':        {SPEED: 400, LANES: 8},
    '400GAUI-4-S':      {SPEED: 400, LANES: 4},
    '400GAUI-4-L':      {SPEED: 400, LANES: 4},
    '400GAUI-2':        {SPEED: 400, LANES: 2},
    '800GAUI-8':        {SPEED: 800, LANES: 8},
    '800GAUI-4':        {SPEED: 800, LANES: 4},
    '1.6TAUI-16-S':     {SPEED: 1600, LANES: 16},
    '1.6TAUI-16-L':     {SPEED: 1600, LANES: 16},
    '1.6TAUI-8':        {SPEED: 1600, LANES: 8},
    '800G':             {SPEED: 800, LANES: 8},
    '10GBASE-CX4':      {SPEED: 10, LANES: 4},
    '25GBASE-CR':       {SPEED: 25, LANES: 1},
    '40GBASE-CR4':      {SPEED: 40, LANES: 4},
    '50GBASE-CR2':      {SPEED: 50, LANES: 2},
    '50GBASE-CR':       {SPEED: 50, LANES: 1},
    '100GBASE-CR10':    {SPEED: 100, LANES: 10},
    '100GBASE-CR4':     {SPEED: 100, LANES: 4},
    '100GBASE-CR2':     {SPEED: 100, LANES: 2},
    '100GBASE-CR1':     {SPEED: 100, LANES: 1},
    '200GBASE-CR4':     {SPEED: 200, LANES: 4},
    '200GBASE-CR2':     {SPEED: 200, LANES: 2},
    '400G':             {SPEED: 400, LANES: 8},
    '400GBASE-CR4':     {SPEED: 400, LANES: 4},
    '800G-ETC-CR8':     {SPEED: 800, LANES: 8},
    '1.6TB-CR16':       {SPEED: 1600, LANES: 16},
    '200GBASE-CR1':     {SPEED: 200, LANES: 1},
    '400GBASE-CR2':     {SPEED: 400, LANES: 2},
    '800GBASE-CR4':     {SPEED: 800, LANES: 4},
    '1.6TBASE-CR8':     {SPEED: 1600, LANES: 8}
}

PORTS_SI_PARAM_NAMES_EXCEL_TO_JSON = {
    SPC3: {
        'ob_amp': 'idriver',
        'ob_pre': 'pre1',
        'ob_pre2': 'pre2',
        'ob_main': 'main',
        'ob_post': 'post1',
        'ob_alev': 'ob_alev_out',
        'ob_m2lp': 'ob_m2lp',
        'obplev': 'obplev',
        'obnlev': 'obnlev',
        'regn_bfm1p': 'regn_bfm1p',
        'regp_bfm1n': 'regn_bfm1n'
    },
    SPC4: {
        'fir_amp': 'idriver',
        'fir_pre1': 'pre1',
        'fir_pre2': 'pre2',
        'fir_pre3': 'pre3',
        'fir_main': 'main',
        'fir_post1': 'post1'
    },
    SPC6: {
        'tap0': 'pre3',
        'tap1': 'pre2',
        'tap2': 'pre1',
        'tap3': 'main',
        'tap4': 'post',
        'tap5': 'post1'
    }
}

# SPC3 has its own module SI columns, all the generations from SPC4 on share these
SPC4_AND_NEWER_MODULE_SI_PARAM_NAMES_EXCEL_TO_JSON = {
    'rx_out_amp': 'OutputAmplitudeTargetRx',
    'rx_out_emp_pre': 'OutputEqPreCursorTargetRx',
    'rx_out_emp_post': 'OutputEqPostCursorTargetRx'
}

MODULE_SI_PARAM_NAMES_EXCEL_TO_JSON = {
    SPC3: {
        'cmis_amp': 'OutputAmplitudeTargetRx',
        'cmis_pre': 'OutputEqPreCursorTargetRx',
        'cmis_post': 'OutputEqPostCursorTargetRx'
    },
    SPC4: SPC4_AND_NEWER_MODULE_SI_PARAM_NAMES_EXCEL_TO_JSON,
    SPC6: SPC4_AND_NEWER_MODULE_SI_PARAM_NAMES_EXCEL_TO_JSON
}

# This dictionary maps between EEPROM entries for the identifier field and their corresponding representation in JSON.
# The JSON identifier representation can be usually derived from the EEPROM, but that is not always the case.
EEPROM_IDENTIFIER_TO_JSON_IDENTIFIER = {'OSFP 8X Pluggable Transceiver': 'OSFP-8X'}


def parse_args():
    """
    @summary: generate the ArgumentParser, parse arguments and return them
    """
    parser = argparse.ArgumentParser(description='Generate Independent Module jsons')
    parser.add_argument('-s', '--spc', type=str.lower, choices=[SPC3, SPC4, SPC5, SPC6], help="Spectrum3, Spectrum4, Spectrum5 or Spectrum6 indication", required=True)
    parser.add_argument('-e', '--eeprom_input', type=str, help="Path to a txt file containing module's EEPROM data")
    parser.add_argument('-m', '--mode', type=str, choices=['port', 'module'], help="The required IM json file - 'port' or 'module' SI data", required=True)
    return parser.parse_args()


def get_groups():
    return SPC_CONFIG[args.spc][PORT_RANGE]


def get_speed_column():
    return SPC_CONFIG[args.spc][SPEED_COLUMN]


def get_excel_row_id(speed):
    generation = LANE_SPEED_TO_GENERATION.get(speed)
    if generation is None:
        return None
    speed_aliases = SPC_CONFIG[args.spc].get(SPEED_ALIASES)
    return generation if speed_aliases is None else speed_aliases.get(generation)


def build_si_param_section(df):
    if args.mode == PORT:
        media_key_to_lane_speed_keys = get_media_key_to_lane_peed_keys_mapping()
        return get_port_si_params(df, media_key_to_lane_speed_keys)
    elif args.mode == MODULE:
        return get_module_si_params(df)


def build_json_dict(si_params):
    ports_range = get_groups()
    json_dict = {
        GLOBAL_MEDIA_SETTINGS: {
            ports_range: si_params
        }
    }
    return json_dict


def export_to_json(json_dict):
    file_name = 'media_settings.json' if args.mode == 'port' else 'optics_si_settings.json'
    with open(file_name, "w") as json_file:
        json.dump(json_dict, json_file, indent=4)
        print("The {} file has been successfully generated".format(file_name))


def get_filtered_df():
    """
    @summary: read the SI datasheet of the requested Spectrum generation and keep only its SerDes device rows
    """
    spc_config = SPC_CONFIG[args.spc]
    if spc_config[SI_PARAMS_EXCEL] is NO_SI_DATASHEET:
        return None
    df = pd.read_excel(spc_config[SI_PARAMS_EXCEL], sheet_name=spc_config[SI_PARAMS_SHEET])
    return df.loc[(df['device'] == spc_config[SERDES_DEVICE]) & (df['Tech'] == ACTIVE)]


# ----------------------------------------------------- Port SI -----------------------------------------------------
def read_eeprom_data_from_file(eeprom_text_file=''):
    eeprom_text_data = []
    if eeprom_text_file:
        if os.path.isfile(eeprom_text_file):
            with open(eeprom_text_file, "r") as eeprom_text_file_fd:
                for line in eeprom_text_file_fd:
                    eeprom_text_data.append(line.strip())
    return eeprom_text_data


def extract_data_from_eeprom(eeprom_data_list, delimiter=':'):
    extracted_data_list = []
    extracted_data = {}
    i = 0

    for line in eeprom_data_list:
        if "SFP EEPROM detected" in line:
            i += 1
            if i > 1:
                extracted_data_list.append(extracted_data)
                extracted_data = {}
            continue
        if delimiter in line:
            line_splitted = line.split(delimiter)
            spec_comp_processing = False
            for key in EEPROM_DATA_TO_EXTRACT:
                # The field name has to match in full, otherwise 'Extended Identifier' is taken for 'Identifier'
                if key == line_splitted[0].strip():
                    # only Specification compliance needs special treatment over several lines
                    if key in APPLICATION_ADVERTISEMENT:
                        extracted_data[key] = [line_splitted[1].split()[0].strip()]
                        spec_comp_processing = True
                    elif key in IDENTIFIER:
                        id_from_eeprom = line_splitted[1].strip()
                        extracted_data[key] = EEPROM_IDENTIFIER_TO_JSON_IDENTIFIER[id_from_eeprom] if id_from_eeprom in EEPROM_IDENTIFIER_TO_JSON_IDENTIFIER else line_splitted[1].split()[0].strip()
                        pass
                    else:
                        extracted_data[key] = line_splitted[1].strip()
        elif spec_comp_processing:
            extracted_data[APPLICATION_ADVERTISEMENT].append(line.split()[0].strip())
    extracted_data_list.append(extracted_data)
    return extracted_data_list


def generate_media_keys_dict(extracted_data_dict_list=None):
    media_keys_dict = {}
    for extracted_data_dict in extracted_data_dict_list:
        # Only CMIS modules advertise applications, which the port SI settings are keyed by
        missing_data = [key for key in EEPROM_DATA_TO_EXTRACT if not extracted_data_dict.get(key)]
        if missing_data:
            print("Skipping a module with no {}".format(' and no '.join(missing_data)))
            continue
        media_key_spec_identifier = '-'.join([extracted_data_dict[EEPROM_DATA_TO_EXTRACT[1]]
                                                 , extracted_data_dict[EEPROM_DATA_TO_EXTRACT[2]]])
        if media_key_spec_identifier not in media_keys_dict:
            media_keys_dict[media_key_spec_identifier] = set()
        speeds = extracted_data_dict[EEPROM_DATA_TO_EXTRACT[0]]
        for speed in speeds:
            media_keys_dict[media_key_spec_identifier].add(speed)
    return media_keys_dict


def get_media_key_to_lane_peed_keys_mapping():

    if args.eeprom_input:
        eeprom_data = read_eeprom_data_from_file(args.eeprom_input)
        extracted_data_dict_list = extract_data_from_eeprom(eeprom_data)
        media_key_spec_identifiers = generate_media_keys_dict(extracted_data_dict_list)
        return media_key_spec_identifiers
    else:
        raise FileNotFoundError("The eeprom data file is missing.")


def get_application_lane_speed(application):
    lane_speed = None
    if application in APPLICATIONS_DATA_BY_SFF_SPEC:
        application_data = APPLICATIONS_DATA_BY_SFF_SPEC[application]
        lane_speed = application_data[SPEED] // application_data[LANES]
    return lane_speed


def decimal_to_hexadecimal(decimal_value):
    width = 8
    if decimal_value >= 0:
        return format(decimal_value, '#0{}x'.format(width + 2))
    else:   # Negative numbers are converted to two's complement representation
        positive_value = abs(decimal_value)
        complement_value = (1 << (width * 4)) - positive_value
        return format(complement_value, '#0{}x'.format(width))


def get_port_single_param_values(param_value):
    param_dict = OrderedDict()
    for lane in range(NUM_OF_LANES):
        lane_str = '{}{}'.format(LANE_PREFIX, lane)
        param_dict[lane_str] = param_value
    return param_dict


def positive_to_negative(value):
    return -abs(value)


def get_port_json_data(excel_row_dict):
    json_data = {}
    si_param_names = PORTS_SI_PARAM_NAMES_EXCEL_TO_JSON[args.spc]
    for col in excel_row_dict:
        if col in si_param_names:
            param_name_excel = col
            param_name_json = PORTS_SI_PARAM_NAMES_EXCEL_TO_JSON[args.spc][param_name_excel]

            param_val_excel = int(excel_row_dict[col])
            json_value = param_val_excel

            # SPC3 conversions
            if param_name_excel == 'ob_pre':
                json_value = positive_to_negative(param_val_excel)
            elif param_name_excel == 'ob_m2lp':
                json_value = param_val_excel - 10
            elif param_name_excel == 'ob_post':
                if excel_row_dict['tx_mode'] == 0 or excel_row_dict['tx_mode'] == 2:
                    json_value = positive_to_negative(param_val_excel)
                elif excel_row_dict['tx_mode'] == 5:
                    json_value = param_val_excel
            # SPC4 conversions
            elif param_name_excel == 'fir_pre3':
                json_value = positive_to_negative(param_val_excel)
            elif param_name_excel == 'fir_pre1':
                json_value = positive_to_negative(param_val_excel)
            elif param_name_excel == 'fir_post1':
                json_value = positive_to_negative(param_val_excel)

            hex_json_value = decimal_to_hexadecimal(json_value)
            json_data[param_name_json] = get_port_single_param_values(hex_json_value)

    return json_data


def get_passive_json_data():
    return {param_name: get_port_single_param_values(decimal_to_hexadecimal(param_value))
            for param_name, param_value in PASSIVE_SI_PARAMS.items()}


def get_port_si_params(df, media_key_to_lane_speed_keys):
    passive_min_lane_speed = SPC_CONFIG[args.spc][PASSIVE_MIN_LANE_SPEED]
    si_params = {}
    for media_key, applications in media_key_to_lane_speed_keys.items():
        is_passive = media_key.endswith(PASSIVE_COPPER_SPEC_COMPLIANCE)
        if is_passive and passive_min_lane_speed is PASSIVE_SKIP:
            print("Skipping '{}', passive SI settings are not maintained for {}".format(media_key, args.spc))
            continue
        if not is_passive and df is None:
            print("Skipping '{}', the {} SI datasheet active modules are taken from is not available"
                  .format(media_key, args.spc))
            continue
        media_key_si_params = {}
        for application in applications:
            lane_speed = get_application_lane_speed(application)
            if lane_speed is None:
                continue
            application_json_str = SPEED_PREFIX + application
            if is_passive:
                if lane_speed >= passive_min_lane_speed:
                    media_key_si_params[application_json_str] = get_passive_json_data()
                continue
            row_id = get_excel_row_id(lane_speed)
            application_si_excel_data = df.loc[(df[get_speed_column()] == row_id) & (df['group'] == GROUP_ID)]
            if application_si_excel_data.empty:
                print("Skipping '{}' of '{}', the {} SI datasheet has no row for a {}G lane speed"
                      .format(application, media_key, args.spc, lane_speed))
                continue
            application_si_excel_data_dict = application_si_excel_data.to_dict(orient='records')[0]
            application_si_json_data = get_port_json_data(application_si_excel_data_dict)

            media_key_si_params[application_json_str] = application_si_json_data
        if media_key_si_params:
            si_params[media_key] = media_key_si_params
    return si_params
# ----------------------------------------------------- Port SI -----------------------------------------------------


# ---------------------------------------------------- Module SI ----------------------------------------------------
def get_module_single_param_values(attribute, param_value):
    param_dict = OrderedDict()
    for lane in range(1, NUM_OF_LANES + 1):
        lane_str = '{}{}'.format(attribute, lane)
        param_dict[lane_str] = param_value
    return param_dict


def get_module_json_data(excel_row_dict):
    json_data = {}
    si_param_names = MODULE_SI_PARAM_NAMES_EXCEL_TO_JSON[args.spc]
    for col in excel_row_dict:
        if col in si_param_names:
            param_name_excel = col
            param_name_json = MODULE_SI_PARAM_NAMES_EXCEL_TO_JSON[args.spc][param_name_excel]
            param_val_excel = int(excel_row_dict[col])
            json_value = param_val_excel
            json_data[param_name_json] = get_module_single_param_values(param_name_json, json_value)

    return json_data


def get_module_si_params(df):
    si_params = {}
    for supported_speed, _ in LANE_SPEED_TO_GENERATION.items():
        lane_speed_si_params = {}
        row_id = get_excel_row_id(supported_speed)
        if not row_id:
            continue
        generation_si_excel_data = df.loc[(df[get_speed_column()] == row_id) & (df['group'] == GROUP_ID)]
        if generation_si_excel_data.empty:
            continue
        generation_si_excel_data_dict = generation_si_excel_data.to_dict(orient='records')[0]
        application_si_json_data = get_module_json_data(generation_si_excel_data_dict)

        lane_speed_str = str(supported_speed) + MODULE_LANE_SPEED_SUFFIX
        lane_speed_si_params[DEFAULT] = application_si_json_data
        si_params[lane_speed_str] = lane_speed_si_params
    return si_params
# ---------------------------------------------------- Module SI ----------------------------------------------------


if __name__ == '__main__':
    args = parse_args()
    df = get_filtered_df()
    si_params_section = build_si_param_section(df)
    json_dict = build_json_dict(si_params_section)
    export_to_json(json_dict)

