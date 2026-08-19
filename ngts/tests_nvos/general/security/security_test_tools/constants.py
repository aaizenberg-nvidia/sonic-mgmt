# aaa constants


class AaaConsts:
    USER = 'user'
    PASSWORD = 'password'
    ROLE = 'role'
    ADMIN = 'admin'
    MONITOR = 'monitor'
    USERNAME = 'username'

    LOCALADMIN = 'localadmin'
    LOCALMONITOR = 'localmonitor'
    STRONG_PASSWORD = 'Your_password1'

    SERVER = 'server'
    TIMEOUT = 'timeout'
    AUTH_TYPE = 'auth-type'
    AUTHENTICATION_MODE = 'mode'
    SERVER_AUTH_MODE = 'auth-mode'
    SECRET = 'secret'
    PORT = 'port'
    RETRANSMIT = 'retransmit'
    PRIORITY = 'priority'
    STATISTICS = 'statistics'

    IPV4 = 'ipv4'
    IPV6 = 'ipv6'
    DN = 'dn'

    MIN_PORT = 1
    MAX_PORT = 65535
    AAA_SERVER_BAD_PORT = 123

    PAP = 'pap'
    CHAP = 'chap'
    MSCHAPV2 = 'mschapv2'
    LOGIN = 'login'

    PHYSICAL_AAA_SERVER_IPV4_ADDR = '10.7.34.20'
    PHYSICAL_AAA_SERVER_IPV6_ADDR = 'fdfd:fdfd:7:34:46a8:42ff:fe27:8ab4'
    PHYSICAL_AAA_SERVER_DN = 'fit-def-20'

    # VM-based AAA servers (tacacs/radius/ldap) are deployed in two labs.
    # The same logical servers exist in both; the location is resolved at runtime
    # (see AaaServerLocation) so the test targets the instance reachable from the DUT.
    # Yokneam lab (legacy/default):
    VM_AAA_SERVER_IPV4_ADDR = '10.237.0.86'
    VM_AAA_SERVER_IPV6_ADDR = 'fdfd:fdfd:10:237:250:56ff:fe1b:56'
    VM_AAA_SERVER_DN = 'fit-l-vrt-60-086.mtl.labs.mlnx'
    # Mevo Carmel lab:
    MEC_VM_AAA_SERVER_IPV4_ADDR = '10.220.163.99'
    MEC_VM_AAA_SERVER_IPV6_ADDR = '2604:a140:6200:204:250:56ff:fe1f:a363'
    MEC_VM_AAA_SERVER_DN = 'fit-nos-vrt-mec-80-099'

    ENABLED = 'enabled'
    DISABLED = 'disabled'


class CLAaaConsts:
    """Cumulus Linux AAA Constants - Updated server configuration for testing"""
    # New working RADIUS/TACACS/LDAP server for CL testing
    PHYSICAL_AAA_SERVER_IPV4_ADDR = '10.237.0.86'
    PHYSICAL_AAA_SERVER_IPV6_ADDR = 'fdfd:fdfd:10:237:250:56ff:fe1b:56'
    PHYSICAL_AAA_SERVER_DN = 'fit-l-vrt-60-086.mtl.labs.mlnx'


class UserRole:
    ADMIN = 'admin'
    MONITOR = 'monitor'
    ALL_ROLES = [ADMIN, MONITOR]


# aaa authentication constants
class AuthConsts:
    AUTHENTICATION = 'authentication'
    ORDER = 'order'
    LOCAL = 'local'
    LDAP = 'ldap'
    RADIUS = 'radius'
    TACACS = 'tacacs'
    FALLBACK = 'fallback'
    FAILTHROUGH = 'failthrough'

    SHOW_COMMAND = 'nv show system'
    SET_COMMAND = 'nv set system security password-hardening len-min 6'
    SWITCH_PROMPT_PATTERN = '.+@.+:.+\\$'
    PERMISSION_ERROR = 'Error: You do not have permission to execute that command.'

    LOGIN_FAIL_ERR = 'login fail'
    SHOW_FAIL_ERR = 'show fail'
    SET_FAIL_ERR = 'set fail'
    UNSET_FAIL_ERR = 'unset fail'

    USERS = 'users'

    # path consts
    SHARED_VERIFICATION_SECURITY_DIR = '/auto/sw_system_project/NVOS_INFRA/security/verification'
    SHARED_VERIFICATION_SCP_DIR = f'{SHARED_VERIFICATION_SECURITY_DIR}/scp/scp_test_files'
    SHARED_VERIFICATION_SCP_UPLOAD_TEST_FILE_NAME = 'shared_scp_upload_test_file.txt'

    SWITCH_SCP_TEST_DIR = '/tmp/scp_test_dir'
    SWITCH_ADMINS_DIR = f'{SWITCH_SCP_TEST_DIR}/admin_users_dir'
    SWITCH_MONITORS_DIR = f'{SWITCH_SCP_TEST_DIR}/non_privileged_dir'
    SWITCH_SCP_DOWNLOAD_TEST_FILE_NAME = 'switch_scp_download_test_file.txt'
    SWITCH_ADMIN_SCP_DOWNLOAD_TEST_FILE = f'{SWITCH_ADMINS_DIR}/{SWITCH_SCP_DOWNLOAD_TEST_FILE_NAME}'
    SWITCH_MONITOR_SCP_DOWNLOAD_TEST_FILE = f'{SWITCH_MONITORS_DIR}/{SWITCH_SCP_DOWNLOAD_TEST_FILE_NAME}'

    SWITCH_ROOT_DIR = '/etc'
    SWITCH_ROOT_FILE_NAME = 'shadow'


class AddressingType:
    IPV4 = 'ipv4'
    IPV6 = 'ipv6'
    DN = 'dn'
    ALL_TYPES = [IPV4, IPV6, DN]


class AaaServerLocation:
    """Labs where the VM AAA servers are deployed, and their per-location addresses.

    The same logical VM AAA server is mirrored in two labs. Test definitions reference the Yokneam
    addresses by default, but a given DUT can only reach the instance in its own lab (e.g. IPv6 to the
    Yokneam server is dead from a Mevo Carmel DUT). The DUT's lab is resolved at runtime (from the
    Noga 'Common.Site' attribute already loaded in the topology) and the server address is swapped to
    the reachable instance.
    """
    YOKNEAM = 'Yokneam'
    MEVO_CARMEL = 'Mevo Carmel'
    ALL_LOCATIONS = [YOKNEAM, MEVO_CARMEL]

    # Prefix of the Noga 'Common.Site' value that identifies the Mevo Carmel lab (e.g. 'MEC01').
    # Yokneam devices report site 'MTL'. Matching is case-insensitive on this prefix.
    MEVO_CARMEL_SITE_TOKEN = 'MEC'

    VM_SERVER_ADDR_BY_LOCATION = {
        YOKNEAM: {
            AddressingType.IPV4: AaaConsts.VM_AAA_SERVER_IPV4_ADDR,
            AddressingType.IPV6: AaaConsts.VM_AAA_SERVER_IPV6_ADDR,
            AddressingType.DN: AaaConsts.VM_AAA_SERVER_DN,
        },
        MEVO_CARMEL: {
            AddressingType.IPV4: AaaConsts.MEC_VM_AAA_SERVER_IPV4_ADDR,
            AddressingType.IPV6: AaaConsts.MEC_VM_AAA_SERVER_IPV6_ADDR,
            AddressingType.DN: AaaConsts.MEC_VM_AAA_SERVER_DN,
        },
    }

    # Some VM AAA containers are published on a different host port per lab.
    # Server definitions carry the Yokneam (default) port; this maps the
    # per-lab override keyed by the container's docker_name. Containers (or
    # labs) not listed here keep the port from their server definition.
    VM_SERVER_PORT_BY_LOCATION = {
        MEVO_CARMEL: {
            'nvos_tacacs2': 54,
        },
    }

    # SSH credentials (env-var names) used to manage the *host* that runs the VM
    # AAA docker containers, per lab. The host differs per lab, so the login
    # differs too: Yokneam (and any non-listed/unknown host) uses the default
    # TEST_SERVER_* account, while Mevo Carmel requires the VM_* account (the
    # TEST_SERVER_* account is not in the docker group / sudoers there).
    DEFAULT_HOST_SSH_CREDS_ENV = ('TEST_SERVER_USER', 'TEST_SERVER_PASSWORD')
    HOST_SSH_CREDS_ENV_BY_LOCATION = {
        MEVO_CARMEL: ('VM_USER', 'VM_PASSWORD'),
    }

    @classmethod
    def port_override_for(cls, location: str, docker_name: str):
        """Return the per-lab host port for a VM AAA container, or None if unchanged.

        @param location: one of cls.ALL_LOCATIONS
        @param docker_name: the container's docker_name (may be '')
        @return: int port override, or None when the definition's port should be kept
        """
        if not docker_name:
            return None
        return cls.VM_SERVER_PORT_BY_LOCATION.get(location, {}).get(docker_name)

    @classmethod
    def location_from_address(cls, address: str):
        """Reverse-map a VM AAA server address (ipv4/ipv6/dn) to its lab location.

        @param address: server address as used by the test (ipv4/ipv6/dn)
        @return: matching location from cls.ALL_LOCATIONS, or None when the
            address is not a known VM AAA server address (e.g. physical server)
        """
        for location, addresses in cls.VM_SERVER_ADDR_BY_LOCATION.items():
            if address in addresses.values():
                return location
        return None

    @classmethod
    def addressing_type_from_address(cls, address: str):
        """Reverse-map a VM AAA server address to its AddressingType (ipv4/ipv6/dn).

        Recognizes the address in any lab (not just Yokneam), mirroring
        location_from_address(). Returns None for non-VM / unknown addresses.

        @param address: server address as used by the test (ipv4/ipv6/dn)
        @return: matching AddressingType, or None when the address is not a
            known VM AAA server address (e.g. physical server)
        """
        location = cls.location_from_address(address)
        if location is None:
            return None
        addresses = cls.VM_SERVER_ADDR_BY_LOCATION[location]
        return next(addr_type for addr_type, addr in addresses.items() if addr == address)

    @classmethod
    def host_ssh_creds_env_for_address(cls, address: str):
        """Return the (user_env, password_env) names to SSH into the host of a VM AAA server.

        The host that runs the AAA docker containers - and therefore the SSH
        login used to manage them - differs per lab. Falls back to the default
        TEST_SERVER_* creds for non-VM / unknown addresses.

        @param address: server address (ipv4/ipv6/dn)
        @return: tuple (user_env_name, password_env_name)
        """
        location = cls.location_from_address(address)
        return cls.HOST_SSH_CREDS_ENV_BY_LOCATION.get(location, cls.DEFAULT_HOST_SSH_CREDS_ENV)

    @classmethod
    def location_from_site(cls, site: str) -> str:
        """Map a Noga 'Common.Site' value to a lab location, defaulting to YOKNEAM when unknown.

        Mevo Carmel devices report a site like 'MEC01'; Yokneam devices report 'MTL'.

        @param site: Noga Common.Site value (case-insensitive); '' / None -> YOKNEAM
        @return: one of cls.ALL_LOCATIONS
        """
        if (site or '').strip().upper().startswith(cls.MEVO_CARMEL_SITE_TOKEN):
            return cls.MEVO_CARMEL
        return cls.YOKNEAM


class AuthType:
    PAP = 'pap'
    CHAP = 'chap'
    # MSCHAPV2 = 'mschapv2'
    LOGIN = 'login'
    ALL_TYPES = [PAP, CHAP, LOGIN]


class AuthMode:
    PAP = 'pap'
    CHAP = 'chap'
    LOGIN = 'login'
    ALL_TYPES = [PAP, CHAP, LOGIN]


class AuthMedium:
    # possible authentication mediums
    SSH = 'SSH'
    OPENAPI = 'OpenApi'
    RCON = 'RCON'
    SCP = 'SCP'
    ALL_MEDIUMS = [SSH, OPENAPI, RCON, SCP]


class AccountingConsts:
    ACCOUNTING = 'accounting'
    STATE = 'state'
    DISABLED = 'disabled'
    ENABLED = 'enabled'

    VALUES = {
        STATE: [DISABLED, ENABLED]
    }

    DEFAULT = {
        STATE: DISABLED
    }


class AccountingFields:
    STATE = AccountingConsts.STATE
    ALL_FIELDS = [STATE]
