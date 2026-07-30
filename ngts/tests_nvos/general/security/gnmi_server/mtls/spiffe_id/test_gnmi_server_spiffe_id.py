import os
import random
import time
from typing import Dict, List

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import UserRole
from ngts.nvos_tools.infra.CmdRunner import CmdRunner
from ngts.nvos_tools.infra.NvCommand import NvCommand
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.helpers import (
    generate_rand_spiffe_id,
)
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.test_api_mtls_spiffe_id import (
    Case,
    CertInfo,
    RebootTestFlowType,
    SecurityMode,
    TestSetup,
    UserInfo,
    get_dut_hostname,
    get_scp_player,
    import_cas_safely,
    import_certs_safely,
    setup_test,
)
from ngts.tests_nvos.general.security.gnmi_server.mtls.spiffe_id.conftest import (
    system_cleanup,
    cleanup_spiffe_gnmi,
)
from ngts.tests_nvos.general.security.helpers import (
    generate_certs,
    get_test_certs_dir_location,
    set_new_random_users,
)
from ngts.tests_nvos.system.gnmi.conftest import gnmi_certs, scp_player
from ngts.tests_nvos.system.gnmi.constants import CERTIFICATE, GnmiMode
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient, GnmicCmdBuilder
from ngts.tests_nvos.system.gnmi.helpers import build_gnmic_cmd_and_verify, wait_for_gnmi_ready
from ngts.tests_nvos.system.gnmi.test_gnmi_cert import (
    read_process_for_specified_time,
    validate_gnmi_streaming_output,
)
from ngts.tools.test_utils import allure_utils as allure


def setup_gnmi_security_mode(mode: str, server_cert: CertInfo, server_ca: CertInfo):
    system = System()
    system.gnmi_server.unset().verify_result()
    if mode != SecurityMode.UNSECURED:
        system.gnmi_server.set("certificate", server_cert.name).verify_result()
    if mode == SecurityMode.MTLS:
        system.gnmi_server.mtls.set(
            "ca-certificate", server_ca.cacert_name
        ).verify_result()
    system._general_cli_wrapper.apply_config(
        TestToolkit.engines.dut, verify_execution=True
    )


@pytest.mark.security
@pytest.mark.mtls
@pytest.mark.gnmi
def test_gnmi_spiffe_core_functionality(dut_hostname, engines, scp_player, system_cleanup):
    """
    preparation for test:
    - 3 local users (usr1, usr2, usr3)
    - one CA that issues all client certs (for server)
    - client certs:
        - cert without spiffe
        - cert0 with spiffe that no user will have
        - cert11 with spiffe11 that usr1 will have
        - cert12 with spiffe12 that usr1 will have
        - cert2 with spiffe2 that usr2 will have
    - prepare gnmi configuration as the required security mode
        - unsecured: no further configuration
        - tls: import server cert and bind to gnmi
        - mtls: as tls + import client CA and bind to gnmi-server mtls ca-certificate
    """
    setup: TestSetup = setup_test(dut_hostname, engines, scp_player)

    bad_pw = "bad-password"
    user1_bad_pw = UserInfo(setup.user1.username, bad_pw, setup.user1.role)
    user2_bad_pw = UserInfo(setup.user2.username, bad_pw, setup.user2.role)
    user3_bad_pw = UserInfo(setup.user3.username, bad_pw, setup.user3.role)

    no_mtls_cases: List[Case] = [
        # no creds
        # name, creds, cert, expected user
        Case("no creds + no cert", None, None, None),
        Case(f"no creds + {setup.cert_no_spif.info}", None, setup.cert_no_spif, None),
        Case(f"no creds + {setup.cert_2_spifs.info}", None, setup.cert_2_spifs, None),
        Case(
            f"no creds + {setup.cert_spif_not_exists.info}",
            None,
            setup.cert_spif_not_exists,
            None,
        ),
        Case(
            f"no creds + {setup.cert_spif_of_user1_1.info}",
            None,
            setup.cert_spif_of_user1_1,
            None,
        ),
        Case(
            f"no creds + {setup.cert_spif_of_user1_2.info}",
            None,
            setup.cert_spif_of_user1_2,
            None,
        ),
        Case(
            f"no creds + {setup.cert_spif_of_user2.info}",
            None,
            setup.cert_spif_of_user2,
            None,
        ),
        # usr1 good pw
        Case("usr1 good creds + no cert", setup.user1, None, setup.user1),
        Case(
            f"usr1 good creds + {setup.cert_no_spif.info}",
            setup.user1,
            setup.cert_no_spif,
            setup.user1,
        ),
        Case(
            f"usr1 good creds + {setup.cert_2_spifs.info}",
            setup.user1,
            setup.cert_2_spifs,
            setup.user1,
        ),
        Case(
            f"usr1 good creds + {setup.cert_spif_not_exists.info}",
            setup.user1,
            setup.cert_spif_not_exists,
            setup.user1,
        ),
        Case(
            f"usr1 good creds + {setup.cert_spif_of_user1_1.info}",
            setup.user1,
            setup.cert_spif_of_user1_1,
            setup.user1,
        ),
        Case(
            f"usr1 good creds + {setup.cert_spif_of_user1_2.info}",
            setup.user1,
            setup.cert_spif_of_user1_2,
            setup.user1,
        ),
        Case(
            f"usr1 good creds + {setup.cert_spif_of_user2.info}",
            setup.user1,
            setup.cert_spif_of_user2,
            setup.user1,
        ),
        # usr1 bad pw
        Case("usr1 bad creds + no cert", user1_bad_pw, None, None),
        Case(
            f"usr1 bad creds + {setup.cert_2_spifs.info}",
            user1_bad_pw,
            setup.cert_2_spifs,
            None,
        ),
        Case(
            f"usr1 bad creds + {setup.cert_no_spif.info}",
            user1_bad_pw,
            setup.cert_no_spif,
            None,
        ),
        Case(
            f"usr1 bad creds + {setup.cert_spif_not_exists.info}",
            user1_bad_pw,
            setup.cert_spif_not_exists,
            None,
        ),
        Case(
            f"usr1 bad creds + {setup.cert_spif_of_user1_1.info}",
            user1_bad_pw,
            setup.cert_spif_of_user1_1,
            None,
        ),
        Case(
            f"usr1 bad creds + {setup.cert_spif_of_user1_2.info}",
            user1_bad_pw,
            setup.cert_spif_of_user1_2,
            None,
        ),
        Case(
            f"usr1 bad creds + {setup.cert_spif_of_user2.info}",
            user1_bad_pw,
            setup.cert_spif_of_user2,
            None,
        ),
        # usr2 good pw
        Case("usr2 good creds + no cert", setup.user2, None, setup.user2),
        Case(
            f"usr2 good creds + {setup.cert_2_spifs.info}",
            setup.user2,
            setup.cert_2_spifs,
            setup.user2,
        ),
        Case(
            f"usr2 good creds + {setup.cert_no_spif.info}",
            setup.user2,
            setup.cert_no_spif,
            setup.user2,
        ),
        Case(
            f"usr2 good creds + {setup.cert_spif_not_exists.info}",
            setup.user2,
            setup.cert_spif_not_exists,
            setup.user2,
        ),
        Case(
            f"usr2 good creds + {setup.cert_spif_of_user1_1.info}",
            setup.user2,
            setup.cert_spif_of_user1_1,
            setup.user2,
        ),
        Case(
            f"usr2 good creds + {setup.cert_spif_of_user1_2.info}",
            setup.user2,
            setup.cert_spif_of_user1_2,
            setup.user2,
        ),
        Case(
            f"usr2 good creds + {setup.cert_spif_of_user2.info}",
            setup.user2,
            setup.cert_spif_of_user2,
            setup.user2,
        ),
        # usr2 bad pw
        Case("usr2 bad creds + no cert", user2_bad_pw, None, None),
        Case(
            f"usr2 bad creds + {setup.cert_2_spifs.info}",
            user2_bad_pw,
            setup.cert_2_spifs,
            None,
        ),
        Case(
            f"usr2 bad creds + {setup.cert_no_spif.info}",
            user2_bad_pw,
            setup.cert_no_spif,
            None,
        ),
        Case(
            f"usr2 bad creds + {setup.cert_spif_not_exists.info}",
            user2_bad_pw,
            setup.cert_spif_not_exists,
            None,
        ),
        Case(
            f"usr2 bad creds + {setup.cert_spif_of_user1_1.info}",
            user2_bad_pw,
            setup.cert_spif_of_user1_1,
            None,
        ),
        Case(
            f"usr2 bad creds + {setup.cert_spif_of_user1_2.info}",
            user2_bad_pw,
            setup.cert_spif_of_user1_2,
            None,
        ),
        Case(
            f"usr2 bad creds + {setup.cert_spif_of_user2.info}",
            user2_bad_pw,
            setup.cert_spif_of_user2,
            None,
        ),
        # usr3 good pw
        Case("usr3 good creds + no cert", setup.user3, None, setup.user3),
        Case(
            f"usr3 good creds + {setup.cert_2_spifs.info}",
            setup.user3,
            setup.cert_2_spifs,
            setup.user3,
        ),
        Case(
            f"usr3 good creds + {setup.cert_no_spif.info}",
            setup.user3,
            setup.cert_no_spif,
            setup.user3,
        ),
        Case(
            f"usr3 good creds + {setup.cert_spif_not_exists.info}",
            setup.user3,
            setup.cert_spif_not_exists,
            setup.user3,
        ),
        Case(
            f"usr3 good creds + {setup.cert_spif_of_user1_1.info}",
            setup.user3,
            setup.cert_spif_of_user1_1,
            setup.user3,
        ),
        Case(
            f"usr3 good creds + {setup.cert_spif_of_user1_2.info}",
            setup.user3,
            setup.cert_spif_of_user1_2,
            setup.user3,
        ),
        Case(
            f"usr3 good creds + {setup.cert_spif_of_user2.info}",
            setup.user3,
            setup.cert_spif_of_user2,
            setup.user3,
        ),
        # usr3 bad pw
        Case("usr3 bad creds + no cert", user3_bad_pw, None, None),
        Case(
            f"usr3 bad creds + {setup.cert_2_spifs.info}",
            user3_bad_pw,
            setup.cert_2_spifs,
            None,
        ),
        Case(
            f"usr3 bad creds + {setup.cert_no_spif.info}",
            user3_bad_pw,
            setup.cert_no_spif,
            None,
        ),
        Case(
            f"usr3 bad creds + {setup.cert_spif_not_exists.info}",
            user3_bad_pw,
            setup.cert_spif_not_exists,
            None,
        ),
        Case(
            f"usr3 bad creds + {setup.cert_spif_of_user1_1.info}",
            user3_bad_pw,
            setup.cert_spif_of_user1_1,
            None,
        ),
        Case(
            f"usr3 bad creds + {setup.cert_spif_of_user1_2.info}",
            user3_bad_pw,
            setup.cert_spif_of_user1_2,
            None,
        ),
        Case(
            f"usr3 bad creds + {setup.cert_spif_of_user2.info}",
            user3_bad_pw,
            setup.cert_spif_of_user2,
            None,
        ),
    ]

    mtls_cases: List[Case] = [
        # no creds
        Case("no creds + no cert", None, None, None),
        Case(f"no creds + {setup.cert_2_spifs.info}", None, setup.cert_2_spifs, None),
        Case(f"no creds + {setup.cert_no_spif.info}", None, setup.cert_no_spif, None),
        Case(
            f"no creds + {setup.cert_spif_not_exists.info}",
            None,
            setup.cert_spif_not_exists,
            None,
        ),
        Case(
            f"no creds + {setup.cert_spif_of_user1_1.info}",
            None,
            setup.cert_spif_of_user1_1,
            setup.user1,
        ),
        Case(
            f"no creds + {setup.cert_spif_of_user1_2.info}",
            None,
            setup.cert_spif_of_user1_2,
            setup.user1,
        ),
        Case(
            f"no creds + {setup.cert_spif_of_user2.info}",
            None,
            setup.cert_spif_of_user2,
            setup.user2,
        ),
        # usr1 good pw
        Case("usr1 good creds + no cert", setup.user1, None, None),
        Case(
            f"usr1 good creds + {setup.cert_2_spifs.info}",
            setup.user1,
            setup.cert_2_spifs,
            None,
        ),
        Case(
            f"usr1 good creds + {setup.cert_no_spif.info}",
            setup.user1,
            setup.cert_no_spif,
            None,
        ),
        Case(
            f"usr1 good creds + {setup.cert_spif_not_exists.info}",
            setup.user1,
            setup.cert_spif_not_exists,
            None,
        ),
        Case(
            f"usr1 good creds + {setup.cert_spif_of_user1_1.info}",
            setup.user1,
            setup.cert_spif_of_user1_1,
            setup.user1,
        ),
        Case(
            f"usr1 good creds + {setup.cert_spif_of_user1_2.info}",
            setup.user1,
            setup.cert_spif_of_user1_2,
            setup.user1,
        ),
        Case(
            f"usr1 good creds + {setup.cert_spif_of_user2.info}",
            setup.user1,
            setup.cert_spif_of_user2,
            setup.user2,
        ),
        # usr1 bad pw
        Case("usr1 bad creds + no cert", user1_bad_pw, None, None),
        Case(
            f"usr1 bad creds + {setup.cert_2_spifs.info}",
            user1_bad_pw,
            setup.cert_2_spifs,
            None,
        ),
        Case(
            f"usr1 bad creds + {setup.cert_no_spif.info}",
            user1_bad_pw,
            setup.cert_no_spif,
            None,
        ),
        Case(
            f"usr1 bad creds + {setup.cert_spif_not_exists.info}",
            user1_bad_pw,
            setup.cert_spif_not_exists,
            None,
        ),
        Case(
            f"usr1 bad creds + {setup.cert_spif_of_user1_1.info}",
            user1_bad_pw,
            setup.cert_spif_of_user1_1,
            setup.user1,
        ),
        Case(
            f"usr1 bad creds + {setup.cert_spif_of_user1_2.info}",
            user1_bad_pw,
            setup.cert_spif_of_user1_2,
            setup.user1,
        ),
        Case(
            f"usr1 bad creds + {setup.cert_spif_of_user2.info}",
            user1_bad_pw,
            setup.cert_spif_of_user2,
            setup.user2,
        ),
        # usr2 good pw
        Case("usr2 good creds + no cert", setup.user2, None, None),
        Case(
            f"usr2 good creds + {setup.cert_2_spifs.info}",
            setup.user2,
            setup.cert_2_spifs,
            None,
        ),
        Case(
            f"usr2 good creds + {setup.cert_no_spif.info}",
            setup.user2,
            setup.cert_no_spif,
            None,
        ),
        Case(
            f"usr2 good creds + {setup.cert_spif_not_exists.info}",
            setup.user2,
            setup.cert_spif_not_exists,
            None,
        ),
        Case(
            f"usr2 good creds + {setup.cert_spif_of_user1_1.info}",
            setup.user2,
            setup.cert_spif_of_user1_1,
            setup.user1,
        ),
        Case(
            f"usr2 good creds + {setup.cert_spif_of_user1_2.info}",
            setup.user2,
            setup.cert_spif_of_user1_2,
            setup.user1,
        ),
        Case(
            f"usr2 good creds + {setup.cert_spif_of_user2.info}",
            setup.user2,
            setup.cert_spif_of_user2,
            setup.user2,
        ),
        # usr2 bad pw
        Case("usr2 bad creds + no cert", user2_bad_pw, None, None),
        Case(
            f"usr2 bad creds + {setup.cert_2_spifs.info}",
            user2_bad_pw,
            setup.cert_2_spifs,
            None,
        ),
        Case(
            f"usr2 bad creds + {setup.cert_no_spif.info}",
            user2_bad_pw,
            setup.cert_no_spif,
            None,
        ),
        Case(
            f"usr2 bad creds + {setup.cert_spif_not_exists.info}",
            user2_bad_pw,
            setup.cert_spif_not_exists,
            None,
        ),
        Case(
            f"usr2 bad creds + {setup.cert_spif_of_user1_1.info}",
            user2_bad_pw,
            setup.cert_spif_of_user1_1,
            setup.user1,
        ),
        Case(
            f"usr2 bad creds + {setup.cert_spif_of_user1_2.info}",
            user2_bad_pw,
            setup.cert_spif_of_user1_2,
            setup.user1,
        ),
        Case(
            f"usr2 bad creds + {setup.cert_spif_of_user2.info}",
            user2_bad_pw,
            setup.cert_spif_of_user2,
            setup.user2,
        ),
        # usr3 good pw
        Case("usr3 good creds + no cert", setup.user3, None, None),
        Case(
            f"usr3 good creds + {setup.cert_2_spifs.info}",
            setup.user3,
            setup.cert_2_spifs,
            setup.user3,
        ),
        Case(
            f"usr3 good creds + {setup.cert_no_spif.info}",
            setup.user3,
            setup.cert_no_spif,
            setup.user2,
        ),
        Case(
            f"usr3 good creds + {setup.cert_spif_not_exists.info}",
            setup.user3,
            setup.cert_spif_not_exists,
            setup.user2,
        ),
        Case(
            f"usr3 good creds + {setup.cert_spif_of_user1_1.info}",
            setup.user3,
            setup.cert_spif_of_user1_1,
            setup.user1,
        ),
        Case(
            f"usr3 good creds + {setup.cert_spif_of_user1_2.info}",
            setup.user3,
            setup.cert_spif_of_user1_2,
            setup.user1,
        ),
        Case(
            f"usr3 good creds + {setup.cert_spif_of_user2.info}",
            setup.user3,
            setup.cert_spif_of_user2,
            setup.user2,
        ),
        # usr3 bad pw
        Case("usr3 bad creds + no cert", user3_bad_pw, None, None),
        Case(
            f"usr3 bad creds + {setup.cert_2_spifs.info}",
            user3_bad_pw,
            setup.cert_2_spifs,
            None,
        ),
        Case(
            f"usr3 bad creds + {setup.cert_no_spif.info}",
            user3_bad_pw,
            setup.cert_no_spif,
            None,
        ),
        Case(
            f"usr3 bad creds + {setup.cert_spif_not_exists.info}",
            user3_bad_pw,
            setup.cert_spif_not_exists,
            None,
        ),
        Case(
            f"usr3 bad creds + {setup.cert_spif_of_user1_1.info}",
            user3_bad_pw,
            setup.cert_spif_of_user1_1,
            setup.user1,
        ),
        Case(
            f"usr3 bad creds + {setup.cert_spif_of_user1_2.info}",
            user3_bad_pw,
            setup.cert_spif_of_user1_2,
            setup.user1,
        ),
        Case(
            f"usr3 bad creds + {setup.cert_spif_of_user2.info}",
            user3_bad_pw,
            setup.cert_spif_of_user2,
            setup.user2,
        ),
    ]

    cases_by_security_mode: Dict[str, List[Case]] = {
        SecurityMode.UNSECURED: no_mtls_cases,
        SecurityMode.TLS: no_mtls_cases,
        SecurityMode.MTLS: mtls_cases,
    }

    with allure.step("test all cases"):
        for mode in SecurityMode.ALL_MODES:
            with allure.independent_step(mode):
                with allure.step(f"setup security mode: {mode}"):
                    setup_gnmi_security_mode(mode, setup.server_cert, setup.server_ca)
                    time.sleep(5)
                check_test_cases(cases_by_security_mode[mode], setup, mode, engines)


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.mtls
def test_gnmi_persistant(dut_hostname, engines, scp_player, system_cleanup):
    """
    Check gnmi rotation works when changing certificate on fly

    1. Validate that gnmi server in mTLS mode
    2. local user: usr1/pw1 (admin role)
    3. bind spif1 usr1
    4. Validate gnmi works with cert with spif1
    5. Open connection to gnmi with cert with spif1
    6. Unbind spif1 from usr1
    7. Validate gnmi connection still works
    """
    dn = dut_hostname
    ip = engines.dut.ip
    system = system_cleanup

    certs_location = get_test_certs_dir_location("spiffe", dut_hostname)

    with allure.step("generate random spiffe"):
        spiffe = generate_rand_spiffe_id()

    with allure.step("prepare client certs"):
        cn = "nvos-client"
        cert_with_spiffe = CertInfo(
            "cert1", "spiffe of user1", "", "", "", "", dn, ip, "", f"{cn}-1", [spiffe]
        )
        client_certs_dir = os.path.join(certs_location, "client_certs")
        clients_certs = [cert_with_spiffe]
        generate_certs(client_certs_dir, clients_certs)

    with allure.step("prepare server cert"):
        server_certs_dir = os.path.join(certs_location, "server_certs")
        server_cert = CertInfo(
            "server-cert", "server cert", "", "", "", "", dn, ip, "", f"{dn}"
        )
        server_certs = [server_cert]
        generate_certs(server_certs_dir, server_certs)

    with allure.step("set local users"):
        admins = set_new_random_users(1, UserRole.ADMIN)
        user: UserInfo = admins[0]

    with allure.step("apply config"):
        system._general_cli_wrapper.apply_config(engines.dut, verify_execution=True)
    with allure.step("save config"):
        NvueGeneralCli.save_config(engines.dut)

    with allure.step("bind generated spiffe to user"):
        system.aaa.user.user_id[user.username].spiffe_id.spiffe[spiffe].set().verify_result()

    with allure.step("apply all prepared configuration"):
        system._general_cli_wrapper.apply_config(engines.dut, verify_execution=True)

    with allure.step("import server cert & CA"):
        import_certs_safely(server_certs, scp_player)
        import_cas_safely([clients_certs[0]], scp_player)

    setup_gnmi_security_mode(SecurityMode.MTLS, server_certs[0], clients_certs[0])

    with allure.step("Build gnmi process"):
        gnmic = GnmicCmdBuilder(host=ip)
        gnmic_cmd = (
            gnmic.user_creds("", "")
            .ca(server_cert.cacert)
            .cert(cert_with_spiffe.private, cert_with_spiffe.public)
            .subscribe(prefix="platform-general", path="", mode=GnmiMode.STREAM)
            .build()
        )
        cmd_runner = CmdRunner("GnmiClient")
        _, _, gnmi_process = cmd_runner.run_cmd_in_process(
            cmd=gnmic_cmd, keep_process_alive=True
        )

    output, err = read_process_for_specified_time(gnmi_process, GnmiConsts.SLEEP_TIME_FOR_UPDATE)
    validate_gnmi_streaming_output(output, err)

    with allure.step(f"Unbind spiffe from user {user.username}"):
        system.aaa.user.user_id[user.username].spiffe_id.spiffe[cert_with_spiffe.san_uris[0]].unset().verify_result()

    with allure.step("apply config"):
        system._general_cli_wrapper.apply_config(engines.dut, verify_execution=True)

    time.sleep(2)

    with allure.step("Verify still receiving updated after changing to other valid cert"):
        output, err = read_process_for_specified_time(gnmi_process, GnmiConsts.SLEEP_TIME_FOR_UPDATE)
        validate_gnmi_streaming_output(output, err)

    with allure.step("Verify new request with spiffe doesn't work"):
        build_gnmic_cmd_and_verify(
            host=ip,
            creds=None,
            expect_success=False,
            client_ca=server_cert,
            insecured=False,
            client_cert=cert_with_spiffe,
        )


# @pytest.mark.system
# @pytest.mark.gnmi
# @pytest.mark.mtls
# def test_gnmi_stress(dut_hostname, engines, scp_player, system_cleanup):
#     """
#     Check gnmi timing is correct when we have 700 users and search for spiffe
#
#     1. Create 700 random users
#     2. Validate time it takes for gnmi client request
#     3. Bind spiffe to random user
#     4. Validate that gnmi server in mTLS mode
#     5. Validate time it takes for gnmi client request
#     6. Cleanup
#     """
#     system = system_cleanup
#     dn = dut_hostname
#     ip = engines.dut.ip
#
#     with allure.step("set 700 local users"):
#         admins = set_new_random_users(400, UserRole.ADMIN)
#         monitors = set_new_random_users(300, UserRole.MONITOR)
#         admin_user: UserInfo = admins[0]
#         monitor_user: UserInfo = monitors[0]
#
#     with allure.step("apply config"):
#         system._general_cli_wrapper.apply_config(engines.dut, verify_execution=True)
#
#     with allure.step("perform gnmi request"):
#         start_time = time.time()
#         build_gnmic_cmd_and_verify(
#             host=ip,
#             creds=admin_user,
#             expect_success=True,
#             client_ca=None,
#             insecured=True,
#             client_cert=None,
#         )
#         end_time = time.time()
#         print(f"gnmi request time without cert: {end_time - start_time}")
#
#     with allure.step("generate random spiffe"):
#         certs_location = get_test_certs_dir_location("spiffe", dut_hostname)
#         spiffe = generate_rand_spiffe_id()
#
#     with allure.step("prepare client certs"):
#         cn = "nvos-client"
#         ca_cert = CertInfo(
#             "cert", "without spiffe", "", "", "", "", dn, ip, "", f"{cn}"
#         )
#         cert_with_spiffe = CertInfo(
#             "cert1", "spiffe of user1", "", "", "", "", dn, ip, "", f"{cn}-1", [spiffe]
#         )
#         client_certs_dir = os.path.join(certs_location, "client_certs")
#         clients_certs = [cert_with_spiffe]
#         generate_certs(client_certs_dir, clients_certs)
#
#     with allure.step("prepare server cert"):
#         server_certs_dir = os.path.join(certs_location, "server_certs")
#         server_certs = [
#             CertInfo("server-cert", "server cert", "", "", "", "", dn, ip, "", f"{dn}"),
#         ]
#         generate_certs(server_certs_dir, server_certs)
#
#     with allure.step("bind generated spiffe to user"):
#         system.aaa.user.user_id[admin_user.username].spiffe_id.spiffe[
#             spiffe
#         ].set().verify_result()
#
#     with allure.step("apply all prepared configuration"):
#         system._general_cli_wrapper.apply_config(engines.dut, verify_execution=True)
#
#     with allure.step("import server cert & CA"):
#         import_certs_safely(server_certs, scp_player)
#         import_cas_safely([ca_cert], scp_player)
#
#     setup_gnmi_security_mode(SecurityMode.MTLS, cert_with_spiffe, ca_cert)
#
#     with allure.step("perform gnmi request with spiffe"):
#         start_time = time.time()
#         build_gnmic_cmd_and_verify(
#             host=ip,
#             creds=None,
#             expect_success=True,
#             client_ca=server_certs[0],
#             client_cert=cert_with_spiffe,
#         )
#         end_time = time.time()
#         print(f"gnmi request time without cert: {end_time - start_time}")


def check_spiffe_negative(
    engines,
    setup,
    verify_config: bool = True,
    verify_auth: bool = True,
    users_should_exist: bool = True,
):
    if verify_config:
        with allure.step("verify config not kept in show"):
            for user in [setup.user1, setup.user2]:
                with allure.independent_step(user.username):
                    user_obj = System().aaa.user.user_id[user.username]
                    if users_should_exist:
                        out = OutputParsingTool.parse_json_str_to_dictionary(
                            user_obj.spiffe_id.show()
                        ).get_returned_value()
                        expected = {}
                        assert out == expected, (
                            f"output of {user.username} general spiffe resource is not as expected:\nexpected: {expected}\nactual: {out}"
                        )
                    else:
                        user_obj.spiffe_id.show(should_succeed=False)
    if verify_auth:
        with allure.step("verify spiffe auth doesn't work"):
            cases: List[Case] = [
                Case(
                    f"no creds + {setup.cert_spif_of_user1_1.info}",
                    None,
                    setup.cert_spif_of_user1_1,
                    None,
                ),
                Case(
                    f"no creds + {setup.cert_spif_of_user1_2.info}",
                    None,
                    setup.cert_spif_of_user1_2,
                    None,
                ),
                Case(
                    f"no creds + {setup.cert_spif_of_user2.info}",
                    None,
                    setup.cert_spif_of_user2,
                    None,
                ),
            ]
            check_test_cases(cases, setup, SecurityMode.MTLS, engines)


def check_spiffe_positive(
    engines, setup, verify_config: bool = True, verify_auth: bool = True
):
    if verify_config:
        with allure.step("verify config kept in show"):
            for user in [setup.user1, setup.user2]:
                with allure.independent_step(user.username):
                    user_obj = System().aaa.user.user_id[user.username]
                    out = OutputParsingTool.parse_json_str_to_dictionary(
                        user_obj.spiffe_id.show()
                    ).get_returned_value()
                    expected = {spif: {} for spif in setup.spiffes_info[user.username]}
                    assert out == expected, (
                        f"output of {user.username} general spiffe resource is not as expected:\nexpected: {expected}\nactual: {out}"
                    )
    if verify_auth:
        with allure.step("verify spiffe auth works"):
            wait_for_gnmi_ready(engines)
            cases: List[Case] = [
                Case(
                    f"no creds + {setup.cert_spif_of_user1_1.info}",
                    None,
                    setup.cert_spif_of_user1_1,
                    setup.user1,
                ),
                Case(
                    f"no creds + {setup.cert_spif_of_user1_2.info}",
                    None,
                    setup.cert_spif_of_user1_2,
                    setup.user1,
                ),
                Case(
                    f"no creds + {setup.cert_spif_of_user2.info}",
                    None,
                    setup.cert_spif_of_user2,
                    setup.user2,
                ),
            ]
            check_test_cases(cases, setup, SecurityMode.MTLS, engines)


def check_test_cases(cases: List[Case], setup: TestSetup, security_mode, engines):
    with allure.step("check cases"):
        for case in cases:
            with allure.independent_step(case.name):
                client_ca = (
                    None
                    if security_mode == SecurityMode.UNSECURED
                    else setup.server_cert
                )
                case.verify_show(
                    engines.dut.ip,
                    client_ca,
                    security_mode == SecurityMode.UNSECURED,
                    build_gnmic_cmd_and_verify,
                )


@pytest.mark.track_serial_console
@pytest.mark.security
@pytest.mark.mtls
@pytest.mark.parametrize("reboot_flow", random.sample(RebootTestFlowType.ALL_TYPES, 1))
def test_gnmi_spiffe_reboot_case(reboot_flow, engines, scp_player, dut_hostname):
    """
    Verify that saved SPIFFE config kept after reboot

    Steps:
    1. Set mtls mode
    2. Save
    3. Configure spiffe to user
    4. Don't save
    5. Reboot
    6. Verify config not kept in show
    7. Verify mtls passwordless connection using spiffe doesn't work
    """

    is_save_flow = reboot_flow == RebootTestFlowType.WITH_SAVE

    with allure.step("setup"):
        with allure.step("prepare certs, users, spiffes"):
            setup: TestSetup = setup_test(
                dut_hostname, engines, scp_player, not is_save_flow
            )

        with allure.step(f"setup security mode: {SecurityMode.MTLS}"):
            setup_gnmi_security_mode(
                SecurityMode.MTLS, setup.server_cert, setup.server_ca
            )

    if is_save_flow:
        with allure.step("save config"):
            NvueGeneralCli.save_config(engines.dut)

    with allure.step("reboot the system"):
        NvCommand().system.action_reboot(flags='force').verify_result()
        engines.dut.disconnect()

    with allure.step("verify after reboot"):
        with allure.step("wait for gnmi docker to become available"):
            time.sleep(10)
        if is_save_flow:
            check_spiffe_positive(engines, setup)
        else:
            check_spiffe_negative(engines, setup)


def gnmi_spiffe_factory_reset_no_params_check():
    """
    Factory reset [no params, keep only-files]

    Verify that saved SPIFFE config is removed after these reset factory flavors
    """

    engines = TestToolkit.engines
    scp_player = get_scp_player(engines)
    dut_hostname = get_dut_hostname(engines)
    cert_name_prefix = 'gnmi'

    with allure.step("setup"):
        with allure.step("prepare certs, users, spiffes"):
            setup: TestSetup = setup_test(dut_hostname, engines, scp_player, cert_name_prefix=cert_name_prefix)

        with allure.step(f"setup security mode: {SecurityMode.MTLS}"):
            setup_gnmi_security_mode(
                SecurityMode.MTLS, setup.server_cert, setup.server_ca
            )

        with allure.step("save config"):
            NvueGeneralCli.save_config(engines.dut)

    yield  # factory reset

    try:
        with allure.step("verify after factory reset"):
            check_spiffe_negative(engines, setup, users_should_exist=False)
    finally:
        cleanup_spiffe_gnmi()

    yield  # to prevent StopIteration on the 2nd next() call


def gnmi_spiffe_factory_reset_keep_basic_check():
    """
    Factory reset [keep basic]

    Keep basic – keep everything under aaa/user (spiffe included)
    * gnmi config not kept

    Verify that saved SPIFFE config is removed after these reset factory flavors

    Steps:
    1. Set mtls
    2. Set SPIFFE
    3. Save
    4. Do factory reset
    5. Verify SPIFFE config kept in show (mtls might not be kept)
    6. Verify mtls passwordless connection using spiffe doesn't work
    7. Set mtls again
    8. Verify mtls passwordless connection using spiffe works
    """

    engines = TestToolkit.engines
    scp_player = get_scp_player(engines)
    dut_hostname = get_dut_hostname(engines)
    cert_name_prefix = 'gnmi'

    with allure.step("setup"):
        with allure.step("prepare certs, users, spiffes"):
            setup: TestSetup = setup_test(dut_hostname, engines, scp_player, cert_name_prefix=cert_name_prefix)

        with allure.step(f"setup security mode: {SecurityMode.MTLS}"):
            setup_gnmi_security_mode(
                SecurityMode.MTLS, setup.server_cert, setup.server_ca
            )

        with allure.step("save config"):
            NvueGeneralCli.save_config(engines.dut)

    yield  # factory reset

    try:
        with allure.step("verify after factory reset"):
            check_spiffe_positive(engines, setup, verify_auth=False)
            check_spiffe_negative(engines, setup, verify_config=False)

            with allure.step(f"setup security mode again: {SecurityMode.MTLS}"):
                import_certs_safely([setup.server_cert], scp_player)
                import_cas_safely([setup.server_ca], scp_player)
                setup_gnmi_security_mode(
                    SecurityMode.MTLS, setup.server_cert, setup.server_ca
                )

            check_spiffe_positive(engines, setup, verify_config=False)
    finally:
        cleanup_spiffe_gnmi()

    yield  # to prevent StopIteration on the 2nd next() call


def gnmi_spiffe_factory_reset_keep_all_config_check():
    """
    Factory reset – keep all config

    Keep all config – removes everything except for saved configuration
    """

    engines = TestToolkit.engines
    scp_player = get_scp_player(engines)
    dut_hostname = get_dut_hostname(engines)
    cert_name_prefix = 'gnmi'

    with allure.step("setup"):
        with allure.step("prepare certs, users, spiffes"):
            setup: TestSetup = setup_test(dut_hostname, engines, scp_player, cert_name_prefix='gnmi')

        with allure.step(f"setup security mode: {SecurityMode.MTLS}"):
            setup_gnmi_security_mode(
                SecurityMode.MTLS, setup.server_cert, setup.server_ca
            )

        with allure.step("save config"):
            NvueGeneralCli.save_config(engines.dut)

    yield  # factory reset

    try:
        with allure.step("verify after factory reset"):
            check_spiffe_positive(engines, setup)
    finally:
        cleanup_spiffe_gnmi()

    yield  # to prevent StopIteration on the 2nd next() call


def gnmi_spiffe_upgrade_check():
    """
    Upgrade case

    Verify that saved SPIFFE config is kept after upgrade
    """

    engines = TestToolkit.engines
    scp_player = get_scp_player(engines)
    dut_hostname = get_dut_hostname(engines)
    cert_name_prefix = 'gnmi'

    with allure.step("setup"):
        with allure.step("prepare certs, users, spiffes"):
            setup: TestSetup = setup_test(dut_hostname, engines, scp_player, cert_name_prefix=cert_name_prefix)

        with allure.step(f"setup security mode: {SecurityMode.MTLS}"):
            setup_gnmi_security_mode(
                SecurityMode.MTLS, setup.server_cert, setup.server_ca
            )

        with allure.step("save config"):
            NvueGeneralCli.save_config(engines.dut)

    yield  # do upgrade

    try:
        with allure.step("verify after upgrade"):
            with allure.step("take new revision number for testing admin permissions"):
                cert = setup.cert_no_spif.copy()
                cert.update(cacert=setup.server_cert.cacert)

            check_spiffe_positive(engines, setup)
    finally:
        cleanup_spiffe_gnmi()

    yield  # to prevent StopIteration on the 2nd next() call
