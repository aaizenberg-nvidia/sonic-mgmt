"""
Offline checks for align_fw_components.py / align_cpld.py: no BMC, switch or Noga access.

Inventories are real FirmwareInventory snapshots (GET only, 2026-10-13):
juliet-126 (10.7.111.126), rosalind-c03-2156-bmc, prt-sur-135 (10.7.112.135).

Run: python3 -m pytest -c /dev/null --confcutdir=ngts/tools/align_components ngts/tools/align_components
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import align_cpld  # noqa: E402
import align_fw_components as afc  # noqa: E402
import Component  # noqa: E402

INV = "redfish/v1/UpdateService/FirmwareInventory"

JULIET_INV = {
    "CPLD_0": "CPLD000370_REV0700_CPLD000377_REV2100_CPLD000373_REV1300_CPLD000390_REV0400",
    "MGX_FW_BMC_0": "88.0002.1984",
    "MGX_FW_CPU_0": "0ACTV_01.01.030d",
    "MGX_FW_ERoT_BMC_0": "01.04.0055.0000_n04",
    "MGX_FW_ERoT_CPU_0": "01.04.0055.0000_n04",
    "MGX_FW_ERoT_FPGA_0": "01.04.0055.0000_n04",
    "MGX_FW_ERoT_NVSwitch_0": "01.04.0055.0000_n04",
    "MGX_FW_ERoT_NVSwitch_1": "01.04.0055.0000_n04",
    "MGX_FW_FPGA_0": "0.24",
    "MGX_FW_NVSwitch_0": "35_2018_1516",
    "MGX_FW_NVSwitch_1": "35_2018_1516",
}
ROSALIND_INV = {
    "CPLD_0": "CPLD000472_REV0103_CPLD000450_REV0010",
    "MGX_FW_BMC_0": "88.0070.1904",
    "MGX_FW_CPU_0": "0ACTV_01.01.044",
    "MGX_FW_ERoT_BMC_0": "02.00.0048.0000_n05",
    "MGX_FW_ERoT_CPU_0": "02.00.0048.0000_n05",
    "MGX_FW_NVSwitch_0": "41.2018.1720",
    "MGX_FW_NVSwitch_1": "41.2018.1720",
    "MGX_FW_NVSwitch_2": "41.2018.1720",
    "MGX_FW_NVSwitch_3": "41.2018.1720",
    "MGX_FW_NVSwitch_SMA_0": "0020.01.0352.0000",
    "MGX_FW_NVSwitch_SMA_1": "0020.01.0352.0000",
}
PORTIA_INV = {
    "CPLD_0": "CPLD000505_REV0005_CPLD000600_REV0006_CPLD000600_REV0006",
    "FW_BMC_0": "88.0060.2466",
    "FW_BMC_Caliptra_0": "2181.16908293",
    "FW_SMA_0": "0058.02.0102.1000",
    "FW_SMA_1": "0058.02.0084.1000",
    "FW_SMA_2": "0058.02.0084.1000",
}


class FakeBmc:
    """Redfish double: serves an inventory, records multipart updates and resets."""

    def __init__(self, inventory, packages=None):
        self.inventory = dict(inventory)
        self.packages = packages or {}  # install path -> {fw id: version after flash}
        self.flashed = []
        self.resets = []
        self.ip, self.username = "fake-bmc", "root"

    def get_query(self, endpoint):
        endpoint = endpoint.lstrip("/")
        if endpoint == INV:
            return {"Members": [{"@odata.id": f"/{INV}/{fw_id}"} for fw_id in self.inventory]}
        if endpoint.startswith(f"{INV}/"):
            fw_id = endpoint.rsplit("/", 1)[1]
            if fw_id not in self.inventory:
                raise Exception("Request failed with status code: 404, Base.1.19.ResourceMissingAtURI")
            return {"Id": fw_id, "Version": self.inventory[fw_id]}
        if endpoint == "redfish/v1/Managers/BMC_0":
            return {"Status": {"State": "Enabled", "Health": "OK"}}
        raise Exception(f"Request failed with status code: 404 ({endpoint})")

    def post_multipart_update(self, endpoint, file_path, targets_list=None, force_update=True):
        self.flashed.append((file_path, targets_list))
        self.inventory.update(self.packages.get(file_path, {}))
        return {"Id": "0"}

    def post_query(self, endpoint, data=""):
        self.resets.append((endpoint, data))
        return {}, None


class FakeTask:
    def __init__(self, **kwargs):
        pass

    def monitor(self, rf_api):
        pass

    def success(self):
        return True

    def await_action(self):
        return False


def entry(path, version):
    latest = {"path": path, "filename": os.path.basename(path), "version_name": version}
    return {"latest": latest, "previous": dict(latest)}


def target(fw_id):
    return [f"/{INV}/{fw_id}"]


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    monkeypatch.setattr(Component.rf_progress, "Task", FakeTask)


def switch_info(name="dut", bmc_ip="10.0.0.1", opn="No"):
    return {"attributes": {"Common": {"Name": name},
                           "Specific": {"opn": opn, "bmc_ip": bmc_ip, "Hardware_state_details": ""}}}


def run_fw(monkeypatch, bmc, recipe, argv=(), **switch):
    monkeypatch.setattr(afc, "get_switch_info", lambda setup_name: switch_info(**switch))
    monkeypatch.setattr(afc, "get_reachable_rf_api", lambda *args: bmc)
    monkeypatch.setattr(afc, "create_json_dict", lambda path: recipe)
    monkeypatch.setattr(afc, "verify_install_path", lambda path: bool(path) and "missing" not in path)
    monkeypatch.setattr(sys, "argv", ["align_fw_components.py", "--setup_name=fake", *argv])
    afc.start_components_update(afc.parse_args())


# ---------------------------------------------------------------- Juliet
JULIET_BMC, JULIET_BMC_PROD = "/fw/juliet_bmc_dev.fwpkg", "/fw/juliet_bmc_prod.fwpkg"
JULIET_EROT, JULIET_EROT_PROD = "/fw/juliet_erot_dev.fwpkg", "/fw/juliet_erot_prod.fwpkg"
JULIET_BIOS, JULIET_FPGA = "/fw/juliet_bios.fwpkg", "/fw/juliet_fpga.fwpkg"
JULIET_FPGA_ENC = "/fw/juliet_fpga_enc.fwpkg"


def juliet_recipe(bmc="88.0002.1984", erot="01.04.0055.0000_n04", fpga="0.24"):
    dev = {
        "compute_bmc": entry("/fw/compute_bmc.fwpkg", "1.0"),  # not a switch-BMC component: ignored
        "bmc": entry(JULIET_BMC, bmc),
        "ssd": entry("", ""),
        "cpld": entry("/fw/juliet.vme", {"CPLD1": "CPLD000370_REV0700"}),
        "fpga": entry(JULIET_FPGA, fpga),
        "fpga_encrypted": entry(JULIET_FPGA_ENC, fpga),
        "erot": entry(JULIET_EROT, erot),
        "asic": entry("/fw/fw-QTM3.mfa", "35.2018.1516"),
        "bios": entry(JULIET_BIOS, "0ACTV_01.01.030d"),
    }
    prod = dict(dev, bmc=entry(JULIET_BMC_PROD, bmc), erot=entry(JULIET_EROT_PROD, erot))
    return {"prod": prod, "dev": dev}


def test_juliet_aligned_nothing_to_do(monkeypatch):
    bmc = FakeBmc(JULIET_INV)
    run_fw(monkeypatch, bmc, juliet_recipe(), name="juliet-142")
    assert bmc.flashed == [] and bmc.resets == []


def test_juliet_bmc_and_erot_outdated(monkeypatch):
    bmc = FakeBmc(JULIET_INV, {JULIET_BMC: {"MGX_FW_BMC_0": "88.0002.2000"},
                               JULIET_EROT: {"MGX_FW_ERoT_BMC_0": "01.04.0060.0000_n04"}})
    run_fw(monkeypatch, bmc, juliet_recipe(bmc="88.0002.2000", erot="01.04.0060.0000_n04"), name="juliet-142")
    assert bmc.flashed == [(JULIET_BMC, target("MGX_FW_BMC_0")), (JULIET_EROT, [])]
    assert len(bmc.resets) == 1


def test_juliet_non_encrypted_fpga_bmc_ip(monkeypatch):
    bmc = FakeBmc(JULIET_INV, {JULIET_FPGA: {"MGX_FW_FPGA_0": "0.25"}})
    run_fw(monkeypatch, bmc, juliet_recipe(fpga="0.25"), name="juliet-148", bmc_ip="10.7.113.148")
    assert bmc.flashed == [(JULIET_FPGA, target("MGX_FW_FPGA_0"))]


def test_juliet_encrypted_fpga_by_default(monkeypatch):
    bmc = FakeBmc(JULIET_INV, {JULIET_FPGA_ENC: {"MGX_FW_FPGA_0": "0.25"}})
    run_fw(monkeypatch, bmc, juliet_recipe(fpga="0.25"), name="juliet-142")
    assert bmc.flashed == [(JULIET_FPGA_ENC, target("MGX_FW_FPGA_0"))]


def test_juliet_126_takes_prod_bmc_package(monkeypatch):
    bmc = FakeBmc(JULIET_INV, {JULIET_BMC_PROD: {"MGX_FW_BMC_0": "88.0002.2000"}})
    run_fw(monkeypatch, bmc, juliet_recipe(bmc="88.0002.2000"), name="juliet-126")
    assert bmc.flashed == [(JULIET_BMC_PROD, target("MGX_FW_BMC_0"))]


def test_juliet_bmc_path_forces_flash(monkeypatch):
    bmc = FakeBmc(JULIET_INV)
    run_fw(monkeypatch, bmc, juliet_recipe(), argv=["--bmc_path=/fw/custom_bmc.fwpkg"], name="juliet-142")
    assert bmc.flashed == [("/fw/custom_bmc.fwpkg", target("MGX_FW_BMC_0"))]


def test_juliet_host_off_fails_instead_of_skipping(monkeypatch):
    host_off = {fw_id: v for fw_id, v in JULIET_INV.items() if fw_id in ("CPLD_0", "MGX_FW_BMC_0", "MGX_FW_ERoT_BMC_0")}
    with pytest.raises(Exception, match="bios: not in the BMC firmware inventory") as err:
        run_fw(monkeypatch, FakeBmc(host_off), juliet_recipe(), name="juliet-142")
    assert "fpga_encrypted: not in the BMC firmware inventory" in str(err.value)


def test_juliet_missing_package_still_fails(monkeypatch):
    recipe = juliet_recipe(bmc="88.0002.2000")
    recipe["dev"]["bmc"] = entry("/fw/missing_bmc.fwpkg", "88.0002.2000")
    bmc = FakeBmc(JULIET_INV)
    with pytest.raises(Exception, match="missing_bmc"):
        run_fw(monkeypatch, bmc, recipe, name="juliet-142")


# ---------------------------------------------------------------- Rosalind
ROSALIND_SMA = "/fw/rosalind_sma.fwpkg"


def rosalind_recipe(sma="0020.01.0352.0000"):
    comps = {
        "bmc": entry("/fw/rosalind_bmc.fwpkg", "88.0070.1904"),
        "ssd": entry("", ""),
        "cpld": entry("/fw/rosalind.vme", {"CPLD1": "CPLD000472_REV0103", "CPLD2": "CPLD000450_REV0010"}),
        "erot": entry("/fw/rosalind_erot.fwpkg", "02.00.0048.0000_n05"),
        "asic": entry("/fw/fw-QTM5.mfa", "41.2018.1720"),
        "bios": entry("/fw/rosalind_bios.fwpkg", "0ACTV_01.01.044"),
        "sma": entry(ROSALIND_SMA, sma),
        "dpc": entry("/fw/dpc.bin", "1.0"),
    }
    return {"prod": comps, "dev": comps}


def test_rosalind_aligned_nothing_to_do(monkeypatch):
    bmc = FakeBmc(ROSALIND_INV)
    run_fw(monkeypatch, bmc, rosalind_recipe(), name="rosalind-c03-2156")
    assert bmc.flashed == [] and bmc.resets == []


def test_rosalind_sma_outdated(monkeypatch):
    new = "0020.01.0400.0000"
    bmc = FakeBmc(ROSALIND_INV, {ROSALIND_SMA: {"MGX_FW_NVSwitch_SMA_0": new, "MGX_FW_NVSwitch_SMA_1": new}})
    run_fw(monkeypatch, bmc, rosalind_recipe(sma=new), name="rosalind-c03-2156")
    assert bmc.flashed == [(ROSALIND_SMA, [])]
    assert len(bmc.resets) == 1


def test_rosalind_second_sma_outdated(monkeypatch):
    bmc = FakeBmc(dict(ROSALIND_INV, MGX_FW_NVSwitch_SMA_1="0020.01.0300.0000"),
                  {ROSALIND_SMA: {"MGX_FW_NVSwitch_SMA_1": "0020.01.0352.0000"}})
    run_fw(monkeypatch, bmc, rosalind_recipe(), name="rosalind-c03-2156")
    assert bmc.flashed == [(ROSALIND_SMA, [])]


# ---------------------------------------------------------------- Portia NEX surrogate (prt-sur-135)
PORTIA_BMC = "/fw/vru_ast2700_bmc_DEV_88.0060.2469.fwpkg"
PORTIA_SMA = "/fw/mcxn556-mcufw-portia-0058.02.0084.1000-rel-debug.fwpkg"
PORTIA_SMA_ALIGNED = {"FW_SMA_0": "0058.02.0084.1000", "FW_SMA_1": "0058.02.0084.1000",
                      "FW_SMA_2": "0058.02.0084.1000"}
PORTIA_CPLD_BMC = "/fw/portia_surrogate_cpld.fwpkg"
PORTIA_CPLD_VERSIONS = {"CPLD1": "CPLD000505_REV0006", "CPLD2": "CPLD000600_REV0006", "CPLD3": "CPLD000600_REV0006"}
# The live BMC has no CPU entry (BMC bug); like Juliet/Rosalind it must have one, FW_CPU_0 by its id naming.
PORTIA_FIXED_INV = dict(PORTIA_INV, FW_CPU_0="0ACUZ")


def portia_recipe():
    comps = {
        "bmc": entry(PORTIA_BMC, "88.0060.2469"),
        "cpld": entry("/fw/FUI000657_BURN_PORTIA_SURROGATE_CPLD000505_REV0006_CPLD000600_REV0006.vme",
                      {"CPLD1": "CPLD000505_REV0006", "CPLD2": "CPLD000600_REV0006", "CPLD3": "CPLD000600_REV0006"}),
        "asic": entry("/fw/fw-QTM3-rel-35_2018_1910.mfa", "35.2018.1910"),
        "bios": entry("", ""),
        "sma": entry(PORTIA_SMA, "0058.02.0084.1000"),
    }
    return {"prod": comps, "dev": comps}


def test_portia_updates_bmc_and_mixed_sma(monkeypatch):
    bmc = FakeBmc(PORTIA_FIXED_INV, {PORTIA_BMC: {"FW_BMC_0": "88.0060.2469"}, PORTIA_SMA: PORTIA_SMA_ALIGNED})
    run_fw(monkeypatch, bmc, portia_recipe(), name="prt-sur-135", bmc_ip="10.7.112.135")
    assert bmc.flashed == [(PORTIA_BMC, target("FW_BMC_0")), (PORTIA_SMA, [])]
    assert len(bmc.resets) == 1
    assert bmc.inventory["FW_BMC_0"] == "88.0060.2469"


def test_portia_live_bmc_without_cpu_fails_after_power_cycle(monkeypatch):
    bmc = FakeBmc(PORTIA_INV, {PORTIA_BMC: {"FW_BMC_0": "88.0060.2469"}, PORTIA_SMA: PORTIA_SMA_ALIGNED})
    with pytest.raises(RuntimeError, match="CPU firmware inventory not ready"):
        run_fw(monkeypatch, bmc, portia_recipe(), name="prt-sur-135", bmc_ip="10.7.112.135")
    assert bmc.flashed == [(PORTIA_BMC, target("FW_BMC_0")), (PORTIA_SMA, [])]
    assert len(bmc.resets) == 1


def test_portia_cpld_through_bmc_when_package_is_fwpkg(monkeypatch):
    recipe = portia_recipe()
    recipe["dev"] = dict(recipe["dev"], cpld=entry(PORTIA_CPLD_BMC, PORTIA_CPLD_VERSIONS))
    aligned = dict(PORTIA_FIXED_INV, FW_BMC_0="88.0060.2469", **PORTIA_SMA_ALIGNED)
    bmc = FakeBmc(aligned, {PORTIA_CPLD_BMC: {"CPLD_0": "CPLD000505_REV0006_CPLD000600_REV0006_CPLD000600_REV0006"}})
    run_fw(monkeypatch, bmc, recipe, name="prt-sur-135", bmc_ip="10.7.112.135")
    assert bmc.flashed == [(PORTIA_CPLD_BMC, target("CPLD_0"))]
    bmc.flashed.clear()
    run_fw(monkeypatch, bmc, recipe, name="prt-sur-135", bmc_ip="10.7.112.135")
    assert bmc.flashed == []


def test_portia_aligned_nothing_to_do(monkeypatch):
    bmc = FakeBmc(dict(PORTIA_INV, FW_BMC_0="88.0060.2469", **PORTIA_SMA_ALIGNED))
    run_fw(monkeypatch, bmc, portia_recipe(), name="prt-sur-135", bmc_ip="10.7.112.135")
    assert bmc.flashed == [] and bmc.resets == []


def test_portia_skipped_components_are_logged(monkeypatch, capsys):
    bmc = FakeBmc(dict(PORTIA_FIXED_INV, FW_BMC_0="88.0060.2469", **PORTIA_SMA_ALIGNED))
    run_fw(monkeypatch, bmc, portia_recipe(), name="prt-sur-135", bmc_ip="10.7.112.135")
    out = capsys.readouterr().out
    assert "bios: empty package path in the versions json, skipping" in out
    assert "erot: not in the versions json, skipping" in out
    assert f"cpld: {os.path.basename(PORTIA_CPLD)} is not a .fwpkg, left to align_cpld.py" in out


def test_portia_cpld_path_fwpkg_through_bmc(monkeypatch):
    bmc = FakeBmc(PORTIA_FIXED_INV)
    run_fw(monkeypatch, bmc, portia_recipe(), argv=[f"--cpld_path={PORTIA_CPLD_BMC}"],
           name="prt-sur-135", bmc_ip="10.7.112.135")
    assert bmc.flashed == [(PORTIA_CPLD_BMC, target("CPLD_0"))]


def test_portia_cpld_path_vme_left_to_align_cpld(monkeypatch, capsys):
    bmc = FakeBmc(PORTIA_FIXED_INV)  # bmc and sma outdated in the recipe: must not be touched by a cpld-only run
    run_fw(monkeypatch, bmc, portia_recipe(), argv=[f"--cpld_path={PORTIA_CPLD}"],
           name="prt-sur-135", bmc_ip="10.7.112.135")
    assert bmc.flashed == [] and bmc.resets == []
    assert f"cpld: {os.path.basename(PORTIA_CPLD)} is not a .fwpkg, left to align_cpld.py" in capsys.readouterr().out


def test_portia_sma_path_only(monkeypatch):
    bmc = FakeBmc(PORTIA_FIXED_INV)
    run_fw(monkeypatch, bmc, portia_recipe(), argv=[f"--sma_path={PORTIA_SMA}"],
           name="prt-sur-135", bmc_ip="10.7.112.135")
    assert bmc.flashed == [(PORTIA_SMA, [])]


# ---------------------------------------------------------------- CPLD
def run_cpld(monkeypatch, installed, recipe, argv=(), opn="No"):
    """:return: cpldupdate commands sent to the switch and resets sent to the BMC"""
    bmc, ssh = FakeBmc({}), []

    def fake_ssh(self, command):
        ssh.append(command)
        return installed

    monkeypatch.setattr(align_cpld, "get_switch_info", lambda setup_name: switch_info(opn=opn))
    monkeypatch.setattr(align_cpld, "create_json_dict", lambda path: recipe)
    monkeypatch.setattr(align_cpld, "verify_install_path", bool)
    monkeypatch.setattr(align_cpld, "RedFishRestApi", lambda *args: bmc)
    monkeypatch.setattr(Component.CpldComponent, "_run_ssh", fake_ssh)
    monkeypatch.setattr(Component.CpldComponent, "_run_scp", lambda self, remote_path: None)
    monkeypatch.setattr(Component.CpldComponent, "_wait_for_nvos_ready", lambda self: None)
    monkeypatch.setattr(sys, "argv", ["align_cpld.py", "--setup_name=fake", *argv])
    align_cpld.perform_cpld_update(align_cpld.parse_args())
    return [cmd for cmd in ssh if "cpldupdate" in cmd], bmc.resets


def cpld_recipe(path, *versions):
    comps = {"cpld": entry(path, {f"CPLD{i}": v for i, v in enumerate(versions, 1)})}
    return {"prod": comps, "dev": comps}


JULIET_CPLD = ("/fw/FUI000525_BURN_JULIET_TTM_CPLD000370_REV0700_CPLD000377_REV2100_"
               "CPLD000373_REV1300_CPLD000390_REV0400_IPN.vme")
ROSALIND_CPLD = "/fw/FUI000619_BURN_ROSALIND_POR_CPLD000472_REV0103_CPLD000450_REV0010_PROD.vme"
PORTIA_CPLD = "/fw/FUI000657_BURN_PORTIA_SURROGATE_CPLD000505_REV0006_CPLD000600_REV0006.vme"


def test_cpld_juliet_aligned(monkeypatch):
    recipe = cpld_recipe(JULIET_CPLD, "CPLD000370_REV0700", "CPLD000377_REV2100", "CPLD000373_REV1300",
                         "CPLD000390_REV0400")
    assert run_cpld(monkeypatch, JULIET_INV["CPLD_0"], recipe) == ([], [])


def test_cpld_juliet_path_aligned(monkeypatch):
    assert run_cpld(monkeypatch, JULIET_INV["CPLD_0"], {}, argv=[f"--cpld_path={JULIET_CPLD}"]) == ([], [])


def test_cpld_rosalind_aligned(monkeypatch):
    recipe = cpld_recipe(ROSALIND_CPLD, "CPLD000472_REV0103", "CPLD000450_REV0010")
    assert run_cpld(monkeypatch, ROSALIND_INV["CPLD_0"], recipe) == ([], [])


def test_cpld_rosalind_outdated(monkeypatch):
    recipe = cpld_recipe(ROSALIND_CPLD.replace("REV0103", "REV0104"), "CPLD000472_REV0104", "CPLD000450_REV0010")
    flashed, resets = run_cpld(monkeypatch, ROSALIND_INV["CPLD_0"], recipe)
    assert len(flashed) == 1 and len(resets) == 1


def test_cpld_portia_live_state_outdated(monkeypatch):
    recipe = cpld_recipe(PORTIA_CPLD, "CPLD000505_REV0006", "CPLD000600_REV0006", "CPLD000600_REV0006")
    flashed, resets = run_cpld(monkeypatch, PORTIA_INV["CPLD_0"], recipe)
    assert flashed == [f"sudo cpldupdate --gpio --print-progress /tmp/{os.path.basename(PORTIA_CPLD)}"]
    assert len(resets) == 1


def test_cpld_portia_aligned(monkeypatch):
    recipe = cpld_recipe(PORTIA_CPLD, "CPLD000505_REV0006", "CPLD000600_REV0006", "CPLD000600_REV0006")
    installed = "CPLD000505_REV0006_CPLD000600_REV0006_CPLD000600_REV0006"
    assert run_cpld(monkeypatch, installed, recipe) == ([], [])


def test_cpld_fwpkg_left_to_bmc(monkeypatch):
    recipe = cpld_recipe(PORTIA_CPLD_BMC, "CPLD000505_REV0006", "CPLD000600_REV0006", "CPLD000600_REV0006")
    assert run_cpld(monkeypatch, PORTIA_INV["CPLD_0"], recipe) == ([], [])
    assert run_cpld(monkeypatch, PORTIA_INV["CPLD_0"], {}, argv=[f"--cpld_path={PORTIA_CPLD_BMC}"]) == ([], [])


def test_cpld_prod_package_when_noga_opn(monkeypatch):
    opn_cpld = JULIET_CPLD.replace("_IPN.vme", "_OPN.vme")
    recipe = {"prod": {"cpld": entry(opn_cpld, {"CPLD1": "CPLD000370_REV0800"})},
              "dev": {"cpld": entry(JULIET_CPLD, {"CPLD1": "CPLD000370_REV0800"})}}
    flashed, _ = run_cpld(monkeypatch, JULIET_INV["CPLD_0"], recipe, opn="Yes")
    assert flashed == [f"sudo cpldupdate --gpio --print-progress /tmp/{os.path.basename(opn_cpld)}"]
