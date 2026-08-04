import hashlib
import os.path
import traceback

import ecdsa
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.x509 import load_pem_x509_certificate

from ngts.tests_nvos.general.security.bmc.bmc_erot_attestation.client_scripts.verify_spdm_measurements.SPDMMeasurements import \
    SPDMMeasurements
from ngts.tests_nvos.general.security.bmc.bmc_erot_attestation.client_scripts.verify_spdm_measurements.utils import log, \
    step

CLIENT_VERIFICATION_ERR = 'client verification failed'
SPDM_1_2_VERSION = 0x12
SPDM_1_2_SIGNING_PREFIX = b'dmtf-spdm-v1.2.*' * 4
SPDM_MEASUREMENTS_SIGNING_CONTEXT = b'responder-measurements signing'
SPDM_SIGNING_CONTEXT_SIZE = 100
# ECDSA-P384 signing uses SHA-384 even when HashingAlgorithm is SHA-512
# (SHA-512 applies to measurement digests, not the signature transcript hash).
SIGNING_HASH = hashlib.sha384


def load_measurements(filename, output=False):
    meas = SPDMMeasurements(filename)

    no_leftovers = meas.parse()

    if output:
        if no_leftovers:
            log("OK: All data consumed. This is expected.")
        else:
            log(
                "WARNING: Leftover data detected! You have provided more than just signed measurements. Check your input!")
        meas.show_manifest()
        meas.show_req_nonce()
        meas.show_resp_nonce()

    return meas


def measurements_sig_verify(measurements, public_key):
    message = measurements.get_signed_content()
    sig = measurements.get_signature()

    if measurements.spdm_version >= SPDM_1_2_VERSION:
        zero_pad_size = (
            SPDM_SIGNING_CONTEXT_SIZE -
            len(SPDM_1_2_SIGNING_PREFIX) -
            1 -
            len(SPDM_MEASUREMENTS_SIGNING_CONTEXT)
        )
        assert zero_pad_size >= 0, f'SPDM 1.2 signing-context padding is negative ({zero_pad_size})'
        signing_context = (
            SPDM_1_2_SIGNING_PREFIX +
            b'\0' +
            b'\0' * zero_pad_size +
            SPDM_MEASUREMENTS_SIGNING_CONTEXT
        )
        assert len(signing_context) == SPDM_SIGNING_CONTEXT_SIZE, (
            f'SPDM 1.2 signing context length {len(signing_context)} != {SPDM_SIGNING_CONTEXT_SIZE}')
        message = signing_context + SIGNING_HASH(message).digest()

    verifier = ecdsa.VerifyingKey.from_string(public_key, curve=ecdsa.curves.NIST384p)
    verifier.verify(sig, message, SIGNING_HASH)


def load_pubkey(filename):
    # This is a manually extracted pubkey from the BMC measurements, I used it to figure out how to extract a key from the cert.
    # pubkey_test = "0406515db1deb97236302e79aa76fcce3964d4b27c8492e7f3fcca4fb01059d274b9e759af4599fcc6993dcc85ba8b608074297d6e4f0de47e25d1be18f64144c207a9f1bcd6252d9440ec7ebdb473ca63872affc05ea578267d7261f326b00a07"
    # pubkey_test = bytes.fromhex(pubkey_test)

    with open(filename, "rb") as cert_file:
        cert_str = cert_file.read()
    cert_obj = load_pem_x509_certificate(cert_str, default_backend())
    public_key = cert_obj.public_key()
    public_key_bytes = public_key.public_bytes(encoding=serialization.Encoding.DER,
                                               format=serialization.PublicFormat.SubjectPublicKeyInfo)
    # This is a bit of black magic: The extracted, DER encoded key has a prefix before the actual public key bytes. Experimentally, this is the first 23 bytes, and then the key bytes follow.
    pubkey = public_key_bytes[23:]
    # log(pubkey)
    return pubkey


def load_nonce(filename):
    with open(filename, "r") as cert_file:
        nonce = cert_file.read()
    return nonce


def validate_request_nonce_against_expected(meas, nonce_file):
    expected_nonce = load_nonce(nonce_file).strip()
    req_nonce = meas.get_req_nonce().hex().strip()
    log(
        f'\n\n==================\nexpected nonce:\t{expected_nonce}\nactual nonce:\t{req_nonce}\n==================\n\n')
    if req_nonce != expected_nonce:
        log(
            f'{CLIENT_VERIFICATION_ERR}: request nonce is not as expected.\nexpected: {expected_nonce}\nactual: {req_nonce}')
        raise ValueError(
            f'{CLIENT_VERIFICATION_ERR}: request nonce is not as expected.\nexpected: {expected_nonce}\nactual: {req_nonce}')
    else:
        log('request nonce is OK')


# Currently doesn't do a nonce comparison, so nonce_file is not used.
def verify_spdm_measurements(meas_file, key_file, nonce_file=None, output=False):
    log("Verifying measurements from file: " + meas_file)
    log("Using key from leaf cert file: " + key_file)

    try:
        with step('Load measurements from file'):
            meas = load_measurements(meas_file, output)
            if output:
                # Print raw data
                meas.show_data()
                # Print signed content
                meas.show_signed_content()
                # Print signature bytes
                meas.show_signature()

        with step('Load pubkey from leaf certificate file'):
            pubkey = load_pubkey(key_file)

        if nonce_file:
            with step('Validate request nonce against expected'):
                validate_request_nonce_against_expected(meas, nonce_file)

        with step('Verify measurements signature'):
            measurements_sig_verify(meas, pubkey)

        log("SUCCESS: SPDM Measurements verified successfully")
    except Exception as e:
        log(f"ERROR: {str(e)}\n{traceback.format_exc()}")
        raise


if __name__ == '__main__':
    tmp_files_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tmp_files')
    tests_dir = os.path.join(tmp_files_dir, 'tests')

    ###########################
    # Test
    dir = os.path.join(tests_dir, 'juliet-126-24-10-31-reverted-leaf')
    meas_file = os.path.join(dir, 'test-measurements.json')
    key_file = os.path.join(dir, 'test-leaf-cert.pem')
    nonce_file = None  # os.path.join(tmp_files_dir, 'test-nonce.txt')

    # Start praying here! Verifying signature!
    verify_spdm_measurements(meas_file, key_file, nonce_file)

    ###########################
    # # Goodflow 1
    # dir = os.path.join(tests_dir, 'verify_spdm_good_1')
    # meas_file = os.path.join(dir, 'bmc_nvue_meas.json')
    # key_file = os.path.join(dir, 'bmc_nvue_leaf_cert.pem')
    # nonce_file = None
    #
    # # Start praying here! Verifying signature!
    # verify_spdm_measurements(meas_file, key_file, nonce_file)

    ###########################
    # # Badflow 1
    # dir = os.path.join(tests_dir, 'verify_spdm_bad_1')
    # # Load Bad Measurements JSON output file
    # meas_file = os.path.join(dir, 'bmc_nvue_meas_bad_sig.json')
    # key_file = os.path.join(dir, 'bmc_nvue_leaf_cert.pem')
    # nonce_file = None
    #
    # # Keep praying here! Verifying signature!
    # verify_spdm_measurements(meas_file, key_file, nonce_file)
