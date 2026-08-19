from __future__ import annotations

from ngts.nvos_tools.system.Spdm import SpdmComponentFields, SPDMComponents

VALID_NONCE_LEN = 64

NONE = "None"
UNKNOWN = "unknown"
NA = "N/A"
NOT_EMPTY = -1
SPDM_VERSION_1_1_0 = "1.1.0"
SPDM_VERSION_1_2_0 = "1.2.0"
TPM_ALG_SHA_384 = "TPM_ALG_SHA_384"
TPM_ALG_SHA_512 = "TPM_ALG_SHA_512"
TPM_ALG_ECDSA_ECC_NIST_P384 = "TPM_ALG_ECDSA_ECC_NIST_P384"


class SpdmConsts:
    SPDM = "spdm"
    components = fields = SPDMComponents.ALL_SUPPORTED_COMPONENTS

    class Component:
        fields = SpdmComponentFields.ALL_FIELDS

        class Certificates:
            CERT_STRING = "CertificateString"
            CERT_TYPE = "CertificateType"
            CERT_USAGE_TYPES = "CertificateUsageTypes"
            ID = "Id"
            NAME = "Name"
            SPDM = "SPDM"
            fields = [CERT_STRING, CERT_TYPE, CERT_USAGE_TYPES, ID, NAME, SPDM]
            na_values: dict[str, str] = {f: NA for f in fields}

        class Measurements:
            HASHING_ALGO = "HashingAlgorithm"
            SIGNED_MEASUREMENTS = "SignedMeasurements"
            SIGNING_ALGO = "SigningAlgorithm"
            VERSION = "Version"
            fields = [HASHING_ALGO, SIGNED_MEASUREMENTS, SIGNING_ALGO, VERSION]
            # SignedMeasurements may be empty until generate.
            initial_values: dict[str, str] = {
                HASHING_ALGO: TPM_ALG_SHA_384,
                SIGNING_ALGO: TPM_ALG_ECDSA_ECC_NIST_P384,
                VERSION: SPDM_VERSION_1_1_0,
            }
            na_values: dict[str, str] = {f: NA for f in fields}

            @staticmethod
            def get_initial_values(component_name: str) -> dict[str, str]:
                initial_values = SpdmConsts.Component.Measurements.initial_values.copy()
                is_nvswitch_sma = component_name.startswith(SPDMComponents.NVSWITCH_SMA_PREFIX)
                is_rosalind_nvswitch = component_name.startswith(SPDMComponents.ROSALIND_NVSWITCH_PREFIX) and not is_nvswitch_sma
                if is_nvswitch_sma or is_rosalind_nvswitch:
                    initial_values[SpdmConsts.Component.Measurements.VERSION] = SPDM_VERSION_1_2_0
                if is_rosalind_nvswitch:
                    initial_values[SpdmConsts.Component.Measurements.HASHING_ALGO] = TPM_ALG_SHA_512
                return initial_values
