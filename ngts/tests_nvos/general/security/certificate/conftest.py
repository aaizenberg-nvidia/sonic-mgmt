import logging
from typing import List

import pytest

from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.certificate.constants import DUT_IMPORTED_CERTS_PUBLIC_DIR, TestCert
from ngts.tests_nvos.general.security.helpers import add_etc_host_mapping_to_dn, remove_etc_host_mapping_to_dn
from ngts.tests_nvos.system.gnmi.constants import ETC_HOSTS
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)

INSTALLED_CERT_SUFFIX = '.crt'


def _installed_cert_ids_from_dut(engines) -> List[str]:
    """Entity certificate ids from the DUT filesystem.

    Every cert any surface can use - including system-internal and the nmx-c / nmx-t internal
    surfaces - must first be imported into the NVUE cert store, so this directory holds one
    ``<cert-id>.crt`` per imported cert and its basenames are the same ids the certificate table
    lists. Binding to an internal surface only adds a deployed copy under ``/etc/internal/cert``
    or ``/etc/<app>/int_cert``; that copy carries the same id and goes away with the store entity,
    so it is not listed separately.
    """
    output = engines.dut.run_cmd(
        f"sudo find {DUT_IMPORTED_CERTS_PUBLIC_DIR} -maxdepth 1 -type f "
        f"-name '*{INSTALLED_CERT_SUFFIX}' -printf '%f\\n' 2>/dev/null || true")
    return sorted({line.strip()[:-len(INSTALLED_CERT_SUFFIX)] for line in output.splitlines()
                   if line.strip().endswith(INSTALLED_CERT_SUFFIX)})


def _imported_cert_ids(system: System, engines) -> List[str]:
    """Imported entity cert ids; falls back to filesystem if the cert table cannot be listed."""
    result = system.security.certificate.show(if_returned_value=False).ignore_result()
    if result.result:
        return list(OutputParsingTool.parse_json_str_to_dictionary(result.returned_value).get_returned_value())
    cert_ids = _installed_cert_ids_from_dut(engines)
    logger.warning('certificate table unlistable (%s); falling back to DUT filesystem ids: %s',
                   result.info, cert_ids)
    return cert_ids


def _delete_all(cert_ids: List[str], delete_one):
    """Delete every id; attempt all, then raise if any failed."""
    failures = {}
    for cert_id in cert_ids:
        try:
            delete_one(cert_id)
        except Exception as error:
            failures[cert_id] = error
            logger.warning('failed to delete %s: %s', cert_id, error)
    assert not failures, f'failed to delete: {failures}'


def clear_existing_certs(engines):
    system = System()
    with allure.step('delete imported certificates'):
        _delete_all(_imported_cert_ids(system, engines),
                    lambda cert_id: system.security.certificate.cert_id[cert_id].action_delete().verify_result())
    with allure.step('delete imported ca-certificates'):
        cas = OutputParsingTool.parse_json_str_to_dictionary(
            system.security.ca_certificate.show()).get_returned_value()
        _delete_all(list(cas),
                    lambda ca_id: system.security.ca_certificate.cert_id[ca_id].action_delete().verify_result())


@pytest.fixture(scope='function')
def clear_certs(engines):
    clear_existing_certs(engines)
    yield
    System().api.unset(apply=True).verify_result()
    clear_existing_certs(engines)


@pytest.fixture(scope='module', autouse=True)
def clear_certs_session(engines):
    clear_existing_certs(engines)
    yield
    System().api.unset(apply=True).verify_result()
    clear_existing_certs(engines)


@pytest.fixture(scope='module', autouse=True)
def etc_hosts_mapping(engines):
    cert: CertInfo = TestCert.cert_valid_1
    with allure.step(f'add ipv4 mapping of new dut hostname to {ETC_HOSTS}'):
        remove_etc_host_mapping_to_dn(cert.dn)
        add_etc_host_mapping_to_dn(cert.dn, engines.dut.ip)
    yield
    with allure.step(f'remove ipv4 mapping of new dut hostname to {ETC_HOSTS}'):
        remove_etc_host_mapping_to_dn(cert.dn)
