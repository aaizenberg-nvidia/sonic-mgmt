#!/usr/bin/env python
'''
This file contains Python script for triggering SDK health event
'''
import sys

from python_sdk_api.sx_api import *
import argparse

######################################################
#    defines
######################################################
SWID = 0
DEVICE_ID = 1
FW_EVENT_TO_HEALTH_CAUSE = {
    1: SX_HEALTH_CAUSE_FW_FATAL_EVENT_E,
    2: SX_HEALTH_CAUSE_FW_ASSERT_E,
    3: SX_HEALTH_CAUSE_PLL_E
}

ERR_FILE_LOCATION = '/tmp/python_err_log.txt'
parser = argparse.ArgumentParser(description='This example demonstrates how to register, \
                                              activate and handle SDK health events',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--device_id', default=1, type=lambda x: int(x, 0), help='The device id on which the health example will run')
parser.add_argument('--fw_event', type=int, help='The Event ID as listed in the sx_dbg_auto.h file')
args = parser.parse_args()


def trigger():

    print("[+] opening sdk")
    rc, handle = sx_api_open(None)
    print(("sx_api_open handle:0x%x , rc %d " % (handle, rc)))
    if (rc != SX_STATUS_SUCCESS):
        print("Failed to open api handle.\nPlease check that SDK is running.")
        return (rc)

    print("--------------- HOST IFC OPEN------------------------------")
    fd_p = new_sx_fd_t_p()
    rc = sx_api_host_ifc_open(handle, fd_p)
    if rc != SX_STATUS_SUCCESS:
        print(("sx_api_host_ifc_open failed rc %d" % rc))
        return (rc)
    fd = sx_fd_t_p_value(fd_p)
    print(("sx_api_host_ifc_open,fd = %d rc=%d] " % (fd.fd, rc)))

    # trigger a test event which will activate the handler
    sx_dbg_health_event_simulate_params_p = new_sx_dbg_health_event_simulate_params_t_p()
    sx_dbg_health_event_simulate_params_p.cause = FW_EVENT_TO_HEALTH_CAUSE.get(args.fw_event)
    rc = sx_api_dbg_health_event_simulate(handle, SX_ACCESS_CMD_SET, sx_dbg_health_event_simulate_params_p)
    if rc != SX_STATUS_SUCCESS:
        print(("sx_api_dbg_health_event_simulate failed rc %d" % rc))
        return (rc)
    delete_sx_dbg_health_event_simulate_params_t_p(sx_dbg_health_event_simulate_params_p)

    print("[+] close host ifc recv fd")
    rc = sx_api_host_ifc_close(handle, fd_p)
    delete_sx_fd_t_p(fd_p)
    if rc != SX_STATUS_SUCCESS:
        print(("sys.exit with error, rc %d" % rc))
        return (rc)

    print("[+] close sdk")
    rc = sx_api_close(handle)
    if rc != SX_STATUS_SUCCESS:
        print(("sys.exit with error, rc %d" % rc))
        return (rc)
    return (rc)


################################################################################
#                             Main                                             #
################################################################################
if __name__ == "__main__":
    trigger()
