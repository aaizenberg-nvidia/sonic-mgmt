from __future__ import annotations

from Component import Component


class ComponentManager:
    def __init__(self, components: list[Component]):
        self.components = components
        self.errors: list[str] = []

    def perform_update(self):
        was_update_performed = False
        for component in self.components:
            installed_version = component.installed_version
            if installed_version != component.required_version:
                print(f"{component.name} will be updated from {installed_version} to "
                      f"{component.required_version or component.install_path} using {component.install_path}")
                result = component.update()
                if not result:
                    error_msg = f"Update for {component.name} failed."
                    print(error_msg)
                    self.errors.append(error_msg)
                    continue
                was_update_performed = True
            else:
                print(f"No update required for {component.name}. Already at {component.required_version}")
        return was_update_performed

    def get_errors(self) -> list[str]:
        return self.errors

    def perform_pc(self, switch_info):
        assert self.components, "Should be at least one component"
        self.components[0].power_cycle(switch_info)

    def print_installed_versions(self):
        for component in self.components:
            print(f"{component.name} is at version {component.installed_version}")
