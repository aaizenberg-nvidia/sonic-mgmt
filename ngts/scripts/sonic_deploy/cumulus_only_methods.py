import logging
import os

from ngts.nvos_tools.system.System import System

logger = logging.getLogger()


class CumulusInstallationSteps:

    @staticmethod
    def pre_installation_steps(setup_info, base_version='', target_version=''):
        assert target_version, 'Argument "target_version" must be provided for installing Cumulus'

    @staticmethod
    def post_installation_steps(setup_info, is_performance):
        """
        Post-installation steps for NVOS NOS
            Update /etc/sudoers file to permit NOPASSWD for sudo
        """
        root_password = os.getenv("CUMULUS_ROOT_PASSWORD")
        for dut in setup_info['duts']:
            logging.info("Updating /etc/sudoers file to permit NOPASSWD for sudo")
            dut['cli_obj'].update_sudoers_nopasswd()
            logging.info("Updated and verified /etc/sudoers file to permit NOPASSWD for sudo")
            if root_password:
                logging.info("Permitting root login for dut")
                dut['engine'].run_cmd_set(
                    ["sudo passwd root", root_password, root_password],
                    patterns_list=["New password", "Retype new password", "passwd: password updated successfully"]
                )
                system = System()
                system.ssh_server.set(
                    'permit-root-login',
                    'enabled',
                    dut_engine=dut['engine'],
                    apply=True,
                    ask_for_confirmation='-y'
                ).verify_result()
                logging.info("Root login for dut enabled")
            else:
                logging.warning("CUMULUS_ROOT_PASSWORD is not set; skipping root password and root SSH login configuration")
            logging.info("Updating /etc/apt/sources.list")
            CumulusInstallationSteps.update_apt_sources_list(dut)
            if is_performance:
                asic_model = dut['cli'].general.get_asic_model(dut['engine'])
                if asic_model == "Spectrum-6":
                    logging.info("Detaching stale pending NVUE config while preserving Spectrum-6 breakout")
                    dut['cli'].general.detach_config(dut['engine'])
                logging.info(f"Disabling set hostname for performance setups.")
                dut_hostname = dut['dut_alias'].replace("_", "-")
                dut['engine'].run_cmd_set(["nv set interface eth0 ipv4 dhcp-client set-hostname disabled",
                                           f"nv set system hostname {dut_hostname}", "nv config apply -y"],
                                          patterns_list=["applied_and_saved"])
                if asic_model == "Spectrum-6":
                    logging.info("Keeping Spectrum-6 ports in the installed default breakout")
                else:
                    logging.info("Bringing up and unsplitting all the ports")
                    dut['cli'].interface.initialize_physical_ports()
                logging.info("Restarting lldpd to clear LLDP neighbors")
                dut['engine'].run_cmd("sudo systemctl restart lldpd")

    @staticmethod
    def update_apt_sources_list(dut):
        #  below command would enable the bookworm debian repository and replace the cumulus dev repository with release one
        bookworm_repo_enable = "\'s/#\\ *deb/deb/g\'"
        cumulus_dev_repo_disable = "\'2d;3d\'"
        cumulus_release_repo_enable = "\'4i # deb  https://apt.cumulusnetworks.com/repo CumulusLinux-d12-latest cumulus upstream netq\'"
        cmd = f"sudo sed -i  -e {bookworm_repo_enable} -e {cumulus_dev_repo_disable} -e {cumulus_release_repo_enable} /etc/apt/sources.list"
        dut['engine'].run_cmd(cmd)
        logging.info(dut['engine'].run_cmd("sudo cat /etc/apt/sources.list"))
