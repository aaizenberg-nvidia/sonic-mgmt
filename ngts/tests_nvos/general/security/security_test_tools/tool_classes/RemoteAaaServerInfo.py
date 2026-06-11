from __future__ import annotations

import subprocess
import logging
import socket
import copy
import abc
import re

from infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine

from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tools.test_utils import allure_utils as allure
from ..constants import AaaConsts, AddressingType, AaaServerLocation, AuthMedium, UserRole
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.system.Server import ServerId
from ...test_aaa_ldap.constants import LdapConsts
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.system.Ldap import Ldap
from ..tool_classes.UserInfo import UserInfo
from .. import resource_utils

try:
    UsersPerAuthMedium = dict[str, dict[str, list[UserInfo]]]
except TypeError:  # backward compatibility for Python 3.7
    from typing_extensions import TypeAlias
    from typing import Dict, List

    UsersPerAuthMedium: TypeAlias = Dict[str, Dict[str, List[UserInfo]]]

logger = logging.getLogger(__name__)


def ping_server(address: str, count: int = 2, timeout: int = 3, dut_engine=None) -> bool:
    """
    Ping a server to check if it's reachable.

    Args:
        address: IP address (IPv4/IPv6) or hostname to ping
        count: Number of ping attempts
        timeout: Timeout in seconds for the ping command
        dut_engine: Optional DUT engine to run ping from DUT instead of
            test runner

    Returns:
        True if server is reachable, False otherwise
    """
    try:
        is_ipv6 = ":" in address
        ping_cmd = "ping6" if is_ipv6 else "ping"
        cmd_str = f"{ping_cmd} -c {count} -W {timeout} {address}"
        logger.debug(f"Running ping command: {cmd_str}")

        if dut_engine:
            result = dut_engine.run_cmd(cmd_str)
            match = re.search(r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets\s+)?received", result)
            is_reachable = bool(match and int(match.group(2)) > 0)
        else:
            cmd = [ping_cmd, "-c", str(count), "-W", str(timeout), address]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout * count + 5)
            is_reachable = result.returncode == 0

        if is_reachable:
            logger.info(f"Server {address} is reachable")
        else:
            logger.warning(f"Server {address} is not reachable")
        return is_reachable
    except subprocess.TimeoutExpired:
        logger.warning(f"Ping to {address} timed out")
        return False
    except Exception as ex:
        logger.error(f"Failed to ping {address}: {ex}")
        return False


def check_port(address: str, port: int, timeout: int = 3, dut_engine=None) -> bool:
    """
    Check if a specific port is open on the server.

    Args:
        address: IP address (IPv4/IPv6) or hostname to check
        port: Port number to check
        timeout: Timeout in seconds for the connection attempt
        dut_engine: Optional DUT engine to run check from DUT instead of
            test runner

    Returns:
        True if port is open, False otherwise
    """
    try:
        if dut_engine:
            is_ipv6 = ":" in address
            nc_flag = "-6" if is_ipv6 else "-4"
            cmd = f"nc -zv {nc_flag} -w {timeout} {address} {port}"
            logger.debug(f"Running port check command on DUT: {cmd}")
            result = dut_engine.run_cmd(cmd)
            is_open = "succeeded" in result.lower() or "open" in result.lower()
        else:
            is_ipv6 = ":" in address
            family = socket.AF_INET6 if is_ipv6 else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((address, port))
            sock.close()
            is_open = result == 0

        if is_open:
            logger.info(f"Port {port} on {address} is open")
        else:
            logger.warning(f"Port {port} on {address} is closed")
        return is_open
    except socket.timeout:
        logger.warning(f"Connection to {address}:{port} timed out")
        return False
    except socket.gaierror as ex:
        logger.error(f"Failed to resolve {address}: {ex}")
        return False
    except Exception as ex:
        logger.error(f"Failed to check port {port} on {address}: {ex}")
        return False


class RemoteAaaServerInfo(abc.ABC):
    def __init__(
        self,
        hostname,
        priority,
        secret,
        port,
        users: list[UserInfo],
        ipv4_addr: str = "",
        docker_name: str = "",
        users_per_auth_medium: UsersPerAuthMedium = None,
    ):
        self.hostname = hostname
        self.priority = priority
        self.secret = secret
        self.port = port
        self.users = users
        # info for mgmt of server
        self.ipv4_addr = ipv4_addr
        self.docker_name = docker_name

        # validate users_per_auth_medium and set it
        if users_per_auth_medium:
            assert all(key in AuthMedium.ALL_MEDIUMS for key in users_per_auth_medium.keys()), (
                f'invalid "users_per_auth_medium" param: 1st layer keys must be in {AuthMedium.ALL_MEDIUMS}'
            )
            for _, users_per_role in users_per_auth_medium.items():
                assert all(key in UserRole.ALL_ROLES for key in users_per_role.keys()), (
                    f'invalid "users_per_auth_medium" param: 2nd layer keys must be in {UserRole.ALL_ROLES}'
                )
        self.users_per_auth_medium: UsersPerAuthMedium = users_per_auth_medium

    def copy(self, deep=False):
        if deep:
            return copy.deepcopy(self)
        else:
            return copy.copy(self)

    @abc.abstractmethod
    def configure(self, engines, set_explicit_priority=False, apply=False, dut_engine=None):
        ...

    def _configure(
        self,
        engines,
        server_resource_obj: ServerId,
        conf_to_set: dict,
        set_explicit_priority: bool,
        apply: bool,
        dut_engine: ProxySshEngine = None,
    ):
        if set_explicit_priority:
            conf_to_set[AaaConsts.PRIORITY] = self.priority
        resource_utils.configure_resource(
            engines, resource_obj=server_resource_obj, conf=conf_to_set, apply=apply, verify_apply=False, dut_engine=dut_engine
        )

    @abc.abstractmethod
    def make_unreachable(self, engines, apply=False, dut_engine=None):
        ...

    @abc.abstractmethod
    def make_reachable(self, engines, apply=False, dut_engine=None):
        ...

    def _retarget_to_dut_lab(self) -> None:
        """
        Re-target this server's address (in place) to the instance located in the DUT's lab.

        The VM AAA servers are mirrored across labs (Yokneam / Mevo Carmel). Server definitions
        reference the Yokneam addresses by default, but a DUT can only reach the instance in its
        own lab. The lab is resolved from the Noga 'Common.Site' attribute already loaded in the
        topology (no extra Noga call / DNS lookup), and the address is swapped to the same
        addressing type (ipv4/ipv6/dn) in that lab.

        This is a no-op for physical / non-VM servers (their hostname is not a known VM address)
        and on the default Yokneam lab (the address is unchanged). It is idempotent, so it is safe
        to call from both verify_availability() and configure().
        """
        addressing_type = AaaServerLocation.addressing_type_from_address(self.hostname)
        if addressing_type is None:
            return  # not a VM AAA server - leave its address as is

        site = ''
        try:
            dut_attributes = TestToolkit.topology_obj.players['dut']['attributes'].noga_query_data['attributes']
            site = dut_attributes.get('Common', {}).get('Site', '') or ''
        except Exception as ex:
            logger.warning(f'Could not read DUT "Common.Site" from Noga topology data: {ex}')

        location = AaaServerLocation.location_from_site(site)
        addresses_for_location = AaaServerLocation.VM_SERVER_ADDR_BY_LOCATION[location]
        new_hostname = addresses_for_location[addressing_type]
        if new_hostname != self.hostname:
            logger.info(f'Re-targeting VM AAA server "{self.hostname}" -> "{new_hostname}" '
                        f'(lab: "{location}", Noga site: "{site}")')
            self.hostname = new_hostname
        # keep ipv4_addr (used for mgmt/availability fallbacks) aligned with the chosen location
        if self.ipv4_addr:
            self.ipv4_addr = addresses_for_location[AddressingType.IPV4]
        # some containers are published on a different host port per lab - align it too
        port_override = AaaServerLocation.port_override_for(location, self.docker_name)
        if port_override is not None and port_override != self.port:
            logger.info(f'Re-targeting VM AAA server "{self.hostname}" ({self.docker_name}) '
                        f'port {self.port} -> {port_override} (lab: "{location}")')
            self.port = port_override

    def get_ping_address(self) -> str:
        """
        Get the address to use for pinging/checking availability.
        Uses the address provided by the test.

        Returns:
            Address string suitable for ping
        """
        return self.hostname or self.ipv4_addr

    def verify_availability(self, check_service: bool = True, dut_engine=None) -> ResultObj:
        """
        Verify that the AAA server is reachable via ping and optionally
        check if the AAA service port is open. Checks are performed from
        the DUT to ensure the DUT can reach the AAA server.

        Args:
            check_service: If True, also checks if the service port is open.
            dut_engine: Optional DUT engine. If None, uses TestToolkit DUT.

        Returns:
            ResultObj with the result of the verification

        """
        if dut_engine is None:
            dut_engine = TestToolkit.engines.dut
        self._retarget_to_dut_lab()
        address = self.get_ping_address()
        with allure.step(f"Verify AAA server availability from DUT: {address}:{self.port}"):
            is_available = ping_server(address, dut_engine=dut_engine)
            if not is_available:
                return ResultObj(
                    False, f"AAA server {self.hostname} (ping: {address}) is not reachable from DUT. Cannot run good flow test."
                )

            if check_service:
                service_up = check_port(address, self.port, dut_engine=dut_engine)
                if not service_up:
                    return ResultObj(
                        False,
                        f"AAA service on {self.hostname}:{self.port} is not "
                        f"responding from DUT. Server is pingable but service "
                        f"may be down. Cannot run good flow test.",
                    )

            return ResultObj(True, f"AAA server {self.hostname} (ping: {address}) is reachable from DUT and service is responding")


def update_active_aaa_server(item, server: RemoteAaaServerInfo):
    item.active_remote_aaa_server = server
    if server is None:
        with allure.step("Change active remote auth server to None"):
            item.active_remote_aaa_server = None
            item.active_remote_admin_engine = None
    else:
        with allure.step("Update to new active remote auth server"):
            item.active_remote_auth_server = server
        with allure.step("Create ssh engine with remote admin user"):
            logger.info("Find remote admin user to use")
            remote_admin = [user for user in server.users if user.role == "admin"][0]
            logger.info(f"Create ssh engine with user: {remote_admin.username}")
            item.active_remote_admin_engine = ProxySshEngine(
                device_type=TestToolkit.engines.dut.device_type,
                ip=TestToolkit.engines.dut.ip,
                username=remote_admin.username,
                password=remote_admin.password,
            )


class TacacsServerInfo(RemoteAaaServerInfo):
    def __init__(
        self,
        hostname,
        priority,
        secret,
        port,
        timeout,
        auth_mode,
        users: list[UserInfo],
        ipv4_addr: str = "",
        docker_name: str = "",
        users_per_auth_mode: dict[str, list[UserInfo]] = None,
        users_per_auth_medium: UsersPerAuthMedium = None,
    ):
        super().__init__(hostname, priority, secret, port, users, ipv4_addr, docker_name, users_per_auth_medium)
        self.timeout = timeout
        # self.retransmit = retransmit
        self.auth_mode = auth_mode
        self.users_per_auth_mode = users_per_auth_mode

    def configure(self, engines, set_explicit_priority=False, apply=False, dut_engine=None):
        self._retarget_to_dut_lab()
        conf_to_set = {
            AaaConsts.SECRET: self.secret,
            AaaConsts.PORT: self.port,
            AaaConsts.TIMEOUT: self.timeout,
            AaaConsts.SERVER_AUTH_MODE: self.auth_mode,
            # AaaConsts.RETRANSMIT: server.retransmit
        }
        server_resource_obj = System().aaa.tacacs.server.server_id[self.hostname]
        self._configure(engines, server_resource_obj, conf_to_set, set_explicit_priority, apply, dut_engine)

    def make_unreachable(self, engines, apply=False, dut_engine=None):
        System().aaa.tacacs.server.server_id[self.hostname].set(
            AaaConsts.PORT, AaaConsts.AAA_SERVER_BAD_PORT, apply=apply, dut_engine=dut_engine
        ).ignore_result()

    def make_reachable(self, engines, apply=False, dut_engine=None):
        System().aaa.tacacs.server.server_id[self.hostname].set(
            AaaConsts.PORT, self.port, apply=apply, dut_engine=dut_engine
        ).ignore_result()

    def update_auth_mode(self, auth_mode: str, item, dut_engine=None, set_on_dut: bool = True):
        logger.info(f'Update server info of "{self.hostname} - {self.port}" users to use {auth_mode} passwords')
        self.auth_mode = auth_mode
        self.users = self.users_per_auth_mode[auth_mode]

        if self.users_per_auth_medium:
            self.__update_passwords_of_users_per_auth_medium(auth_mode)

        if set_on_dut:
            assert item, "argument 'item' was not provided"
            engine = dut_engine or (item.active_remote_admin_engine if hasattr(item, "active_remote_admin_engine") else None)
            System().aaa.tacacs.server.server_id[self.hostname].set(
                AaaConsts.SERVER_AUTH_MODE, auth_mode, apply=True, dut_engine=engine
            ).ignore_result()

    def __update_passwords_of_users_per_auth_medium(self, new_auth_mode):
        for medium, users_per_role in self.users_per_auth_medium.items():
            for role, users in users_per_role.items():
                for user in users:
                    user.password = f"{user.password.replace('_pap', '').replace('_chap', '').replace('_login', '')}_{new_auth_mode}"


class LdapServerInfo(RemoteAaaServerInfo):
    def __init__(
        self,
        hostname,
        priority,
        secret,
        port,
        users: list[UserInfo],
        base_dn,
        bind_dn,
        timeout_bind,
        timeout_search,
        version,
        ssl_port=636,
        ipv4_addr: str = "",
        docker_name: str = "",
        users_per_auth_medium: UsersPerAuthMedium = None,
    ):
        super().__init__(hostname, priority, secret, port, users, ipv4_addr, docker_name, users_per_auth_medium)
        self.base_dn = base_dn
        self.bind_dn = bind_dn
        self.timeout_bind = timeout_bind
        self.timeout_search = timeout_search
        self.version = version
        self.ssl_port = ssl_port

    def configure(self, engines, set_explicit_priority=False, apply=False, dut_engine=None):
        self._retarget_to_dut_lab()
        ldap_obj: Ldap = System().aaa.ldap
        server_resource_obj = ldap_obj.server.server_id[self.hostname]
        server_resource_obj.set(dut_engine=dut_engine).verify_result()
        conf_to_set = {
            LdapConsts.SECRET: self.secret,
            LdapConsts.PORT: self.port,
            LdapConsts.BASE_DN: self.base_dn,
            LdapConsts.BIND_DN: self.bind_dn,
            LdapConsts.VERSION: self.version,
            # LdapConsts.HOSTNAME: self.hostname
        }
        resource_utils.configure_resource(engines, resource_obj=ldap_obj, conf=conf_to_set, apply=False, dut_engine=dut_engine)
        ldap_obj.ssl.set(LdapConsts.SSL_CERT_VERIFY, LdapConsts.DISABLED, dut_engine=dut_engine).verify_result()
        self._configure(engines, server_resource_obj, {}, set_explicit_priority, apply, dut_engine)

    def make_unreachable(self, engines, apply=False, dut_engine=None):
        ldap = System().aaa.ldap
        ldap.server.server_id[self.hostname].unset(apply=False, dut_engine=dut_engine).verify_result()
        ldap.server.server_id["unreachable-" + self.hostname].set(
            AaaConsts.PRIORITY, self.priority, apply=apply, dut_engine=dut_engine
        ).ignore_result()

    def make_reachable(self, engines, apply=False, dut_engine=None):
        ldap = System().aaa.ldap
        ldap.server.server_id["unreachable-" + self.hostname].unset(apply=False, dut_engine=dut_engine).verify_result()
        ldap.server.server_id[self.hostname].set(AaaConsts.PRIORITY, self.priority, apply=apply, dut_engine=dut_engine).ignore_result()


class RadiusServerInfo(RemoteAaaServerInfo):
    def __init__(
        self,
        hostname,
        priority,
        secret,
        port,
        timeout,
        auth_type,
        users: list[UserInfo],
        retransmit=0,
        statistics=AaaConsts.DISABLED,
        ipv4_addr: str = "",
        docker_name: str = "",
        users_per_auth_medium: UsersPerAuthMedium = None,
    ):
        super().__init__(hostname, priority, secret, port, users, ipv4_addr, docker_name, users_per_auth_medium)
        self.timeout = timeout
        self.auth_type = auth_type
        self.retransmit = retransmit
        self.statistics = statistics

    def configure(self, engines, set_explicit_priority=False, apply=False, dut_engine=None):
        self._retarget_to_dut_lab()
        conf_to_set = {
            AaaConsts.SECRET: self.secret,
            AaaConsts.PORT: self.port,
            AaaConsts.TIMEOUT: self.timeout,
            AaaConsts.AUTH_TYPE: self.auth_type,
            AaaConsts.RETRANSMIT: self.retransmit,
        }
        hostname_resource_obj = System().aaa.radius.server.server_id[self.hostname]
        self._configure(engines, hostname_resource_obj, conf_to_set, set_explicit_priority, apply, dut_engine)

    def verify_availability(self, check_service: bool = True, dut_engine=None) -> ResultObj:
        # RADIUS auth uses UDP, so the shared TCP port probe produces false failures.
        return super().verify_availability(check_service=False, dut_engine=dut_engine)

    def make_unreachable(self, engines, apply=False, dut_engine=None):
        System().aaa.radius.server.server_id[self.hostname].set(
            AaaConsts.PORT, AaaConsts.AAA_SERVER_BAD_PORT, apply=apply, dut_engine=dut_engine
        ).ignore_result()

    def make_reachable(self, engines, apply=False, dut_engine=None):
        System().aaa.radius.server.server_id[self.hostname].set(
            AaaConsts.PORT, self.port, apply=apply, dut_engine=dut_engine
        ).ignore_result()

    def update_auth_type(self, auth_type: str, item, dut_engine=None, set_on_dut: bool = True):
        logger.info(f'Update server info of "{self.hostname} - {self.port}" users to use {auth_type} passwords')
        self.auth_type = auth_type

        if set_on_dut:
            assert item, "argument 'item' was not provided"
            engine = dut_engine or (item.active_remote_admin_engine if hasattr(item, "active_remote_admin_engine") else None)
            System().aaa.radius.server.server_id[self.hostname].set(
                AaaConsts.AUTH_TYPE, auth_type, apply=True, dut_engine=engine
            ).ignore_result()
