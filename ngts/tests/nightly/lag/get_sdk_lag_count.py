#!/usr/bin/env python3

import json
import sys

from python_sdk_api.sx_api import (
    SX_ACCESS_CMD_GET,
    SX_ACCESS_CMD_GET_FIRST,
    SX_STATUS_SUCCESS,
    new_sx_port_log_id_t_arr,
    new_uint32_t_p,
    sx_api_close,
    sx_api_lag_port_group_iter_get,
    sx_api_open,
    sx_port_log_id_t_arr_getitem,
    uint32_t_p_assign,
    uint32_t_p_value,
)


def get_sdk_lag_ids():
    rc, handle = sx_api_open(None)
    if rc != SX_STATUS_SUCCESS:
        raise RuntimeError("Failed to open SDK API, rc={}".format(rc))

    try:
        lag_count_p = new_uint32_t_p()
        uint32_t_p_assign(lag_count_p, 0)
        rc = sx_api_lag_port_group_iter_get(
            handle, SX_ACCESS_CMD_GET, 0, 0, None, None, lag_count_p
        )
        if rc != SX_STATUS_SUCCESS:
            raise RuntimeError("Failed to get SDK LAG count, rc={}".format(rc))

        lag_count = uint32_t_p_value(lag_count_p)
        if lag_count == 0:
            return []

        lag_ids_p = new_sx_port_log_id_t_arr(lag_count)
        rc = sx_api_lag_port_group_iter_get(
            handle, SX_ACCESS_CMD_GET_FIRST, 0, 0, None, lag_ids_p, lag_count_p
        )
        if rc != SX_STATUS_SUCCESS:
            raise RuntimeError("Failed to get SDK LAG IDs, rc={}".format(rc))

        lag_count = uint32_t_p_value(lag_count_p)
        return [
            int(sx_port_log_id_t_arr_getitem(lag_ids_p, index))
            for index in range(lag_count)
        ]
    finally:
        sx_api_close(handle)


def main():
    try:
        lag_ids = get_sdk_lag_ids()
    except Exception as err:
        print("Failed to query SDK LAG state: {}".format(err), file=sys.stderr)
        return 1

    print(json.dumps({"count": len(lag_ids), "lag_ids": lag_ids}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
