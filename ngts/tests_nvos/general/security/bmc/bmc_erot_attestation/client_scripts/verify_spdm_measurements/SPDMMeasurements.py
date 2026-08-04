#!/usr/bin/python3

#
# Copyright (c) 2022-2024, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.
#

import argparse
import base64
import json
from ctypes import (
    c_uint8, c_uint16,
    ARRAY, LittleEndianStructure,
    sizeof
)
from pathlib import Path

import cbor2

from ngts.tests_nvos.general.security.bmc.bmc_erot_attestation.client_scripts.verify_spdm_measurements.utils import log


# SPDM message header
class SPDM_MESSAGE_HEADER(LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ('SPDMVersion', c_uint8),
        ('RequestResponseCode', c_uint8),
        ('Param1', c_uint8),  # Request Attributes
        ('Param2', c_uint8)
    ]


# SPDM request and response codes
SPDM_GET_MEASUREMENTS = 0xe0
SPDM_MEASUREMENTS = 0x60

# SPDM Constants
SUPPORTED_SPDM_VERSIONS = (0x11, 0x12)
SPDM_ECDSA_P384_SIG_BYTE_SZ = 96
SPDM_NONCE_BYTE_SZ = 32
SPDM_OPAQUE_LEN_BYTE_SZ = 2


# SPDM GET_MEASUREMENTS request
class SPDM_GET_MEASUREMENTS_REQUEST(LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ('MsgHeader', SPDM_MESSAGE_HEADER),
        ('Nonce', ARRAY(c_uint8, 32)),
        ('SlotIDParam', c_uint8)
    ]


# SPDM GET_MEASUREMENTS request attributes
SPDM_GET_MEASUREMENTS_ATTRIBUTES_SIGNATURE_REQUESTED = 0x1


# SPDM GET_MEASUREMENTS response
class SPDM_MEASUREMENTS_RESPONSE_HEADER(LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ('MsgHeader', SPDM_MESSAGE_HEADER),
        ('NumBlocks', c_uint8),
        ('RecordLength', ARRAY(c_uint8, 3))
    ]


# SPDM MEASUREMENTS block common header
class SPDM_MEASUREMENT_BLK_COMMON_HEADER(LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ('Index', c_uint8),
        ('MeasurementSpecification', c_uint8),
        ('MeasurementSize', c_uint16)
    ]


# SPDM MEASUREMENTS block DMTF header
class SPDM_MEASUREMENT_BLK_DMTF_SPEC_HEADER(LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ('MeasurementValueType', c_uint8),
        ('MeasurementValueSize', c_uint16)
    ]


# SPDM Measurement value type mask
SPDM_MEASUREMENT_VALUE_TYPE_MASK = 0x80  # Bit7: 0 = Digest, 1 = Bit Stream


# SPDM MEASUREMENTS block
class SPDM_MEASUREMENT_BLK(LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ('Header', SPDM_MEASUREMENT_BLK_COMMON_HEADER),
        ('DMTFHeader', SPDM_MEASUREMENT_BLK_DMTF_SPEC_HEADER),
        ('MeasurementValue', ARRAY(c_uint8, 0))
    ]


class DMTFSpecMeasurement:
    """
    Measurement block attributes
    """

    def __init__(self, mindex, valtype, valsize, val):
        self.index = mindex
        self.valtype = valtype
        self.valsize = valsize
        self.val = bytes(val)

    def get_valbytes(self):
        return self.val

    def get_data(self):
        if self.valtype & SPDM_MEASUREMENT_VALUE_TYPE_MASK == 0:
            mrecord = {  # / measurement-map /
                0: self.index,  # / comid.mkey /
                1: {  # / comid.mval /
                    2: [  # / comid.digests /
                        [7, self.val]  # / sha-384, hash-value /
                    ]
                }
            }
        else:
            mrecord = {  # / measurement-map /
                0: self.index,  # / comid.mkey /
                1: {  # / comid.mval /
                    4: cbor2.CBORTag(560, self.val)  # / comid.raw-value /
                }
            }
        return mrecord

    def log(self):
        log('{:2d}: [0x{:02x}] {}'.format(self.index, self.valtype, self.val.hex()))


class SPDMMeasurements:
    """
    Parse SPDM Measurements
        The input data typically is a signed attestation report (covering
        the measurement request and the response), but can also specify
        an unsigned measurement request and response, or just the response.
    """

    def __init__(self, mfile):
        file_extension = Path(mfile).suffix
        if file_extension == '.json':
            with open(mfile, 'r') as f:
                json_data = json.load(f)
                self.data = bytearray(base64.b64decode(json_data['SignedMeasurements']))
        else:
            with open(mfile, 'r') as f:
                self.data = bytearray(base64.b64decode(f.read()))
        self.measurements = {}
        self.spdm_version = None

    def _find_measurements_request_offset(self):
        """Locate GET_MEASUREMENTS in the transcript (SPDM 1.2 may prepend VCA)."""
        request_size = sizeof(SPDM_GET_MEASUREMENTS_REQUEST)
        response_header_size = sizeof(SPDM_MEASUREMENTS_RESPONSE_HEADER)
        last_candidate = len(self.data) - request_size - response_header_size

        for offset in range(max(0, last_candidate + 1)):
            request = SPDM_MESSAGE_HEADER.from_buffer(self.data, offset)
            if (
                request.SPDMVersion not in SUPPORTED_SPDM_VERSIONS or
                request.RequestResponseCode != SPDM_GET_MEASUREMENTS or
                not request.Param1 & SPDM_GET_MEASUREMENTS_ATTRIBUTES_SIGNATURE_REQUESTED
            ):
                continue

            response_offset = offset + request_size
            response = SPDM_MESSAGE_HEADER.from_buffer(self.data, response_offset)
            if (
                response.SPDMVersion == request.SPDMVersion and
                response.RequestResponseCode == SPDM_MEASUREMENTS
            ):
                return offset

        raise ValueError('Unable to locate SPDM measurements request')

    def parse(self):
        offset = self._find_measurements_request_offset()
        msghdr = SPDM_MESSAGE_HEADER.from_buffer(self.data, offset)
        if msghdr.SPDMVersion not in SUPPORTED_SPDM_VERSIONS:
            raise ValueError('Unable to parse SPDM measurements request')
        self.spdm_version = msghdr.SPDMVersion

        if not msghdr.Param1 & SPDM_GET_MEASUREMENTS_ATTRIBUTES_SIGNATURE_REQUESTED:
            raise ValueError('Requested measurements were not signed!')

        msgsize = sizeof(SPDM_GET_MEASUREMENTS_REQUEST)
        hdrsize = sizeof(SPDM_MESSAGE_HEADER)
        offset += hdrsize
        self.req_nonce = self.data[offset:offset + SPDM_NONCE_BYTE_SZ]
        offset += (msgsize - hdrsize)

        msghdr = SPDM_MESSAGE_HEADER.from_buffer(self.data, offset)
        if msghdr.SPDMVersion != self.spdm_version or msghdr.RequestResponseCode != SPDM_MEASUREMENTS:
            raise ValueError('Unable to parse SPDM measurements response')

        resphdr = SPDM_MEASUREMENTS_RESPONSE_HEADER.from_buffer(self.data, offset)
        num_measurement_blocks = resphdr.NumBlocks
        datalen = int.from_bytes(resphdr.RecordLength, 'little')

        offset += sizeof(SPDM_MEASUREMENTS_RESPONSE_HEADER)
        end_offset = offset + datalen

        blknum = 0
        while (offset < end_offset) and (blknum < num_measurement_blocks):
            mblock = SPDM_MEASUREMENT_BLK.from_buffer(self.data, offset)
            mindex = mblock.Header.Index
            msize = mblock.Header.MeasurementSize
            blknum += 1

            mvaltype = mblock.DMTFHeader.MeasurementValueType
            mvalsize = mblock.DMTFHeader.MeasurementValueSize

            offset += sizeof(SPDM_MEASUREMENT_BLK)
            mval = self.data[offset:offset + mvalsize]
            offset += mvalsize

            m = DMTFSpecMeasurement(mindex, mvaltype, mvalsize, mval)
            self.measurements[mindex] = m

        self.resp_nonce = self.data[offset:offset + SPDM_NONCE_BYTE_SZ]
        offset += SPDM_NONCE_BYTE_SZ

        opaque_len = int.from_bytes(self.data[offset:offset + SPDM_OPAQUE_LEN_BYTE_SZ], 'little')
        offset += SPDM_OPAQUE_LEN_BYTE_SZ
        if (opaque_len > 0):
            self.opaque_data = self.data[offset:offset + opaque_len]
            offset += opaque_len

        self.signed_content = self.data[0:offset]

        self.signature = self.data[offset:offset + SPDM_ECDSA_P384_SIG_BYTE_SZ]
        offset += SPDM_ECDSA_P384_SIG_BYTE_SZ
        total_len = len(self.data)
        if offset == total_len:
            return True
        else:
            return False

    def get_manifest(self):
        return self.measurements

    def show_manifest(self):
        log('SPDM Measurements:')
        for m in self.measurements.values():
            m.log()

    def get_req_nonce(self):
        return self.req_nonce

    def show_req_nonce(self):
        output = self.req_nonce.hex()
        byte_sz = int(len(output) / 2)
        log(f'SPDM REQUESTER Nonce ({byte_sz} bytes):')
        log(output)

    def get_resp_nonce(self):
        return self.resp_nonce

    def show_resp_nonce(self):
        output = self.resp_nonce.hex()
        byte_sz = int(len(output) / 2)
        log(f'SPDM RESPONDER Nonce ({byte_sz} bytes):')
        log(output)

    def get_signature(self):
        return self.signature

    def show_signature(self):
        output = self.signature.hex()
        byte_sz = int(len(output) / 2)
        log(f'SPDM Signature ({byte_sz} bytes):')
        log(output)

    def get_signed_content(self):
        return self.signed_content

    def show_signed_content(self):
        output = self.signed_content.hex()
        byte_sz = int(len(output) / 2)
        log(f'Signed Content ({byte_sz} bytes):')
        log(output)

    def get_data(self):
        return self.data

    def show_data(self):
        output = self.data.hex()
        byte_sz = int(len(output) / 2)
        log(f'Raw Data ({byte_sz} bytes):')
        log(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-f', '--file', dest='mfile', required=True,
                        help='SPDM measurements file (.b64 or .json)')
    args = parser.parse_args()

    measurements = SPDMMeasurements(args.mfile)
    measurements.parse()
    measurements.show_manifest()


if __name__ == '__main__':
    main()
