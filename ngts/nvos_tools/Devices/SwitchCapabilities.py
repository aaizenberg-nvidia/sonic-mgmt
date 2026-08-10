"""
Switch Capability System - SOLID Implementation

Clean, maintainable switch capability system using SOLID principles.
"""

from typing import Any, Callable, Dict, List, Set, Protocol, TYPE_CHECKING
from dataclasses import dataclass, field, replace
from abc import ABC, abstractmethod
import logging

from ngts.nvos_tools.Devices.cpo.CpoTopology import CpoTopology

if TYPE_CHECKING:
    from ngts.nvos_tools.Devices.BaseDevice import BaseSwitch

logger = logging.getLogger()


@dataclass
class CapabilityConfig:
    """Metadata for switch capability configuration."""

    name: str
    excluded_categories: Set[str] = field(default_factory=set)
    cleared_lists: Set[str] = field(default_factory=set)
    unsupported_commands: Set[str] = field(default_factory=set)
    excluded_inventory_keys: Set[str] = field(default_factory=set)
    excluded_health_components: Set[str] = field(default_factory=set)
    set_attributes: Dict[str, Any] = field(default_factory=dict)
    computed_attributes: Dict[str, Callable] = field(default_factory=dict)


class Capability(Protocol):
    """Protocol for switch capabilities."""

    def get_config(self) -> CapabilityConfig: ...


class BaseCapability(ABC):
    """Base class for capabilities."""

    @abstractmethod
    def get_config(self) -> CapabilityConfig:
        pass


class NoPSUCapability(BaseCapability):
    """Capability for switches without PSUs."""

    def get_config(self) -> CapabilityConfig:
        # Generate PSU health components to exclude (PSU1-PSU8 and PSU1/FAN-PSU8/FAN)
        excluded_health = set()
        for i in range(1, 9):  # PSU1 through PSU8
            excluded_health.add(f"PSU{i}")
            excluded_health.add(f"PSU{i}/FAN")

        return CapabilityConfig(
            name="NoPSU",
            excluded_categories={"power"},
            cleared_lists={"psu_list", "psu_fan_list", "platform_env_psu_prop"},
            unsupported_commands={
                "nv show platform ps-redundancy",
                "nv set platform ps-redundancy policy no-redundancy",
                "nv show platform environment psu",
            },
            excluded_inventory_keys={"psu"},
            excluded_health_components=excluded_health,
        )


class NoFanCapability(BaseCapability):
    """Capability for switches without fans."""

    def get_config(self) -> CapabilityConfig:
        # Generate Fan health components to exclude (FAN1/1, FAN1/2, ... FAN10/1, FAN10/2)
        excluded_health = set()
        for i in range(1, 11):  # FAN1 through FAN10
            excluded_health.add(f"FAN{i}/1")
            excluded_health.add(f"FAN{i}/2")

        return CapabilityConfig(
            name="NoFan",
            excluded_categories={"fan"},
            cleared_lists={"fan_list", "fan_led_list"},
            unsupported_commands={"nv show platform environment fan"},
            excluded_inventory_keys={"fan"},
            excluded_health_components=excluded_health,
        )


class CpoCapability(BaseCapability):
    """Capability for CPO-capable switches.

    Sets all CPO per-platform data and computes derived attributes.
    The switch must also implement get_mst_device_for_els_index()
    with its platform-specific MST formula.
    """

    def __init__(
        self,
        els_index_to_ga,
        pmaos_module_offset,
        number_of_lasers_per_els,
        els_port_mapping,
        els_oe_mapping,
    ):
        self._els_index_to_ga = els_index_to_ga
        self._pmaos_module_offset = pmaos_module_offset
        self._number_of_lasers_per_els = number_of_lasers_per_els
        self._els_port_mapping = els_port_mapping
        self._els_oe_mapping = els_oe_mapping

    @staticmethod
    def _compute_els_list(switch_instance):
        return [
            name for name in switch_instance.transceiver_list if name.startswith("els")
        ]

    def get_config(self) -> CapabilityConfig:
        return CapabilityConfig(
            name="CPO",
            set_attributes={
                "els_index_to_ga": self._els_index_to_ga,
                "pmaos_module_offset": self._pmaos_module_offset,
                "number_of_lasers_per_els": self._number_of_lasers_per_els,
                "els_port_mapping": self._els_port_mapping,
                "els_oe_mapping": self._els_oe_mapping,
            },
            computed_attributes={
                "els_list": self._compute_els_list,
            },
        )


class PortiaCpoCapability(BaseCapability):
    """Capability for Gen2 (Portia / QM5 / NVL7) CPO switches.

    Attaches a first-class CpoTopology as ``device.cpo`` (one CPO / vModule per
    ASIC, 4 OEs + 1 ELS each) and exposes flat name projections used by the
    platform / system-health validations. Distinct from the Gen1 Taipan
    ``CpoCapability`` - there is no transceiver_list ELS/OE model here.

    Args:
        cpo_count: Override the number of CPOs. Defaults to the device's
            ``asic_amount`` (1 CPO per ASIC).
        oe_per_cpo / els_per_cpo / lasers_per_els / lanes_per_oe /
        channels_per_cpo: Optional CpoTopology field overrides for a
            future CPO device whose vModule layout differs from the Portia default.
    """

    def __init__(  # noqa: PLR0913 - one keyword per CpoTopology field that can be overridden
        self,
        *,
        cpo_count: int | None = None,
        oe_per_cpo: int | None = None,
        els_per_cpo: int | None = None,
        lasers_per_els: int | None = None,
        lanes_per_oe: int | None = None,
        channels_per_cpo: int | None = None,
    ) -> None:
        self._cpo_count: int | None = cpo_count
        # Collect only the overrides that were explicitly given; the rest keep
        # CpoTopology's defaults.
        self._topology_overrides: dict[str, int] = {
            field: value
            for field, value in (
                ("oe_per_cpo", oe_per_cpo),
                ("els_per_cpo", els_per_cpo),
                ("lasers_per_els", lasers_per_els),
                ("lanes_per_oe", lanes_per_oe),
                ("channels_per_cpo", channels_per_cpo),
            )
            if value is not None
        }

    def _build_topology(self, switch_instance: "BaseSwitch") -> CpoTopology:
        # cpo_count defaults to the device's asic_amount (1 CPO / vModule per ASIC).
        count = (
            self._cpo_count
            if self._cpo_count is not None
            else switch_instance.asic_amount
        )
        topology = CpoTopology(cpo_count=count)
        if self._topology_overrides:
            return replace(topology, **self._topology_overrides)
        return topology

    def get_config(self) -> CapabilityConfig:
        return CapabilityConfig(
            name="PortiaCPO",
            # Order matters: 'cpo' is built first, the projections read it after.
            # Gen2 uses distinct attribute names (laser_source_list, not els_list)
            # so it never collides with the Gen1 Taipan els_list/transceiver model.
            computed_attributes={
                "cpo": self._build_topology,
                "cpo_list": lambda sw: sw.cpo.cpo_names(),
                "laser_source_list": lambda sw: sw.cpo.els_names(),
                "oe_list": lambda sw: sw.cpo.oe_names(),
            },
        )


class SwitchConfigurator(ABC):
    """Abstract configurator interface."""

    @abstractmethod
    def configure(self, switch_instance, config: CapabilityConfig) -> None:
        pass


class CategoryConfigurator(SwitchConfigurator):
    """Handles category configuration."""

    def configure(self, switch_instance, config: CapabilityConfig) -> None:
        if not config.excluded_categories:
            return

        original_categories = getattr(switch_instance, "category_list", [])
        new_categories = [
            cat for cat in original_categories if cat not in config.excluded_categories
        ]

        switch_instance.category_list = new_categories
        self._update_category_dicts(switch_instance, new_categories)
        self._update_stats_files(switch_instance, new_categories)

        logger.info(f"Excluded categories: {config.excluded_categories}")

    def _update_category_dicts(self, switch_instance, category_list: List[str]) -> None:
        """Update category dictionaries."""
        if not hasattr(switch_instance, "category_disabled_dict"):
            return

        new_disabled_dict = {}
        new_default_dict = {}

        for category in category_list:
            if category == "disk":
                disabled_value = getattr(
                    switch_instance, "category_disk_default_disable_dict", {}
                )
                default_value = getattr(
                    switch_instance, "category_disk_default_dict", {}
                )
            else:
                disabled_value = getattr(
                    switch_instance, "category_default_disabled_dict", {}
                )
                default_value = getattr(switch_instance, "category_default_dict", {})

            new_disabled_dict[category] = disabled_value
            new_default_dict[category] = default_value

        switch_instance.category_disabled_dict = new_disabled_dict
        switch_instance.category_list_default_dict = new_default_dict

    def _update_stats_files(self, switch_instance, category_list: List[str]) -> None:
        """Update stats dump files."""
        if not hasattr(switch_instance, "constants"):
            return

        stats_files_map = {
            "cpu": "cpu.csv.gz",
            "disk": "disk.csv.gz",
            "fan": "fan.csv.gz",
            "power": "power.csv.gz",
            "mgmt-interface": "mgmt-interface.csv.gz",
            "temperature": "temperature.csv.gz",
            "voltage": "voltage.csv.gz",
            "asic-power": "asic-power.csv.gz",
        }

        new_stats_files = [
            stats_files_map[cat] for cat in category_list if cat in stats_files_map
        ]
        switch_instance.constants = switch_instance.constants._replace(
            stats_dump_files=new_stats_files
        )


class ListConfigurator(SwitchConfigurator):
    """Handles list clearing."""

    def configure(self, switch_instance, config: CapabilityConfig) -> None:
        for list_name in config.cleared_lists:
            if hasattr(switch_instance, list_name):
                setattr(switch_instance, list_name, [])
                logger.info(f"Cleared list: {list_name}")


class CommandConfigurator(SwitchConfigurator):
    """Handles unsupported commands."""

    def configure(self, switch_instance, config: CapabilityConfig) -> None:
        if not hasattr(switch_instance, "unsupported_commands_list"):
            switch_instance.unsupported_commands_list = []

        for cmd in config.unsupported_commands:
            if cmd not in switch_instance.unsupported_commands_list:
                switch_instance.unsupported_commands_list.append(cmd)
                logger.info(f"Added unsupported command: {cmd}")


class InventoryConfigurator(SwitchConfigurator):
    """Handles inventory configuration."""

    def configure(self, switch_instance, config: CapabilityConfig) -> None:
        if not hasattr(switch_instance, "platform_inventory_items_dict"):
            return

        for key in config.excluded_inventory_keys:
            switch_instance.platform_inventory_items_dict.pop(key, None)
            logger.info(f"Excluded inventory key: {key}")

        self._update_inventory_list(switch_instance)

    def _update_inventory_list(self, switch_instance) -> None:
        """Update platform_inventory_items list."""
        if not hasattr(switch_instance, "platform_inventory_items"):
            return

        new_items = []
        for category, items in switch_instance.platform_inventory_items_dict.items():
            if isinstance(items, list):
                new_items.extend(items)
            else:
                new_items.append(items)
        switch_instance.platform_inventory_items = new_items


class HealthComponentsConfigurator(SwitchConfigurator):
    """Handles health components configuration."""

    def configure(self, switch_instance, config: CapabilityConfig) -> None:
        if not hasattr(switch_instance, "health_components"):
            return

        if not config.excluded_health_components:
            return

        # Filter out excluded health components
        original_components = getattr(switch_instance, "health_components", [])
        new_components = [
            comp
            for comp in original_components
            if comp not in config.excluded_health_components
        ]

        switch_instance.health_components = new_components
        logger.info(f"Excluded health components: {config.excluded_health_components}")


class AttributeConfigurator(SwitchConfigurator):
    """Handles setting static and computed attributes on the switch instance."""

    def configure(self, switch_instance, config: CapabilityConfig) -> None:
        for attr_name, value in config.set_attributes.items():
            setattr(switch_instance, attr_name, value)
            logger.info(f"Set attribute: {attr_name}")
        for attr_name, compute_fn in config.computed_attributes.items():
            setattr(switch_instance, attr_name, compute_fn(switch_instance))
            logger.info(f"Computed attribute: {attr_name}")


class SwitchCapabilityHandler:
    """Main handler for switch capabilities."""

    def __init__(self):
        self.configurators: List[SwitchConfigurator] = [
            CategoryConfigurator(),
            ListConfigurator(),
            CommandConfigurator(),
            InventoryConfigurator(),
            HealthComponentsConfigurator(),
            AttributeConfigurator(),
        ]

    def configure_switch_capabilities(
        self, switch_instance, capabilities: List[Capability]
    ) -> None:
        """
        Configure switch based on its capabilities.

        Args:
            switch_instance: The switch instance to configure
            capabilities: List of specific capabilities to apply
        """
        if not capabilities:
            logger.info("No capabilities provided")
            return

        # Convert capabilities to configs
        configs = [cap.get_config() for cap in capabilities]
        logger.info(
            f"Configuring switch with capabilities: {[c.name for c in configs]}"
        )

        for config in configs:
            for configurator in self.configurators:
                configurator.configure(switch_instance, config)

    @classmethod
    def apply_capability(cls, switch_instance, capability: Capability) -> None:
        """
        Apply a single capability to a switch instance.

        Args:
            switch_instance: The switch instance to configure
            capability: The capability to apply
        """
        handler = cls()
        handler.configure_switch_capabilities(switch_instance, [capability])

    @classmethod
    def apply_capabilities(
        cls, switch_instance, capabilities: List[Capability]
    ) -> None:
        """
        Apply multiple capabilities to a switch instance.

        Args:
            switch_instance: The switch instance to configure
            capabilities: List of capabilities to apply
        """
        handler = cls()
        handler.configure_switch_capabilities(switch_instance, capabilities)
