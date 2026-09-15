from ngts.constants.constants import CliType, IndependentModuleConst, InfraConst


def software_control_warning(hostname):
    return (
        f"Software control is enabled on shared fanout {hostname}. "
        "This can prevent connected interfaces from coming up. "
        f"({IndependentModuleConst.IM_SAI_ATTRIBUTE_NAME}=1)"
    )


def is_shared_fanout(player):
    if not player:
        return False
    labels = player["attributes"].noga_query_data["attributes"]["Common"].get("labels") or []
    return InfraConst.SHARED_FANOUT_LABEL in labels


def software_control_enabled(player):
    attributes = player["attributes"].noga_query_data["attributes"]
    if attributes["Topology Conn."]["CLI_TYPE"] != CliType.SONIC:
        return False
    return bool(player["cli"].im.is_im_enabled())
