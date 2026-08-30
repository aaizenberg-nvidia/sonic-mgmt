# SONiC Deployment Flow Documentation

## Overview

This document describes the refactored deployment flow for the SONiC test infrastructure. The deployment system has been transformed from a monolithic function into a modular, maintainable architecture that supports multiple CLI types through polymorphism.

## Architecture Components

### DeploymentContext

The `DeploymentContext` class serves as the central parameter management system, encapsulating all deployment parameters and derived values in a single, consistent interface.

**Key Features:**
- Centralizes 25+ function parameters into a single context object
- Provides consistent access patterns through properties
- Handles parameter validation and initialization
- Factory method for clean object creation

**Usage:**
```python
context = DeploymentContext.from_function_params(
    topology_obj=topology_obj,
    base_version=base_version,
    target_version=target_version,
    # ... all other parameters
)
```

**Key Properties:**
- `context.all_duts` - List of all DUT devices
- `context.primary_cli_obj` - Primary CLI object for operations
- `context.setup_info` - Topology setup information
- `context.base_version` / `context.target_version` - Version information

### Helper Classes

The deployment logic is organized into specialized helper classes, each with a single responsibility:

#### DeployImageHelper
Handles image-related operations:
- `get_related_image_to_switch()` - Determines appropriate image for switch
- `is_dut_supports_image()` - Validates image compatibility
- `cleanup_reboot_cause_history()` - Cleans up reboot history after upgrades

#### DeployTopologyHelper
Manages topology configurations:
- `filter_testbed_yaml_file()` - Filters and prepares testbed configuration

#### DeployConnectionHelper
Handles connection and logging:
- `handle_serial_log_analyzers()` - Manages serial log analysis cleanup

#### DeployMultiNosHelper
Multi-NOS deployment operations:
- `multi_nos_pre_installation_steps()` - Pre-installation for multiple NOS types
- `get_image_for_traffic_generators()` - Prepares images for traffic generators
- `validate_sudo_config()` - Validates sudo configuration

#### DeployDpuHelper
DPU-specific deployment operations:
- `bfb_install_dpu()` - Installs DPU firmware
- `disable_dark_mode()` - Disables dark mode for specific setups

#### DeployOrchestrator
Coordinates the overall deployment flow:
- `execute_full_deployment()` - Main deployment orchestration
- `execute_pre_installation_steps()` - Pre-installation phase
- `execute_installation()` - Installation phase
- `execute_post_installation_steps()` - Post-installation phase

#### DeploySanityChecker
Handles sanity check operations:
- `test_sanity_checks_after_deploy()` - Runs sanity checks after deployment

## Deployment Flow

### 1. Context Initialization
```python
context = DeploymentContext.from_function_params(**all_parameters)
```
- Creates deployment context with all parameters
- Initializes setup information from topology
- Validates configuration
- Sets up derived values and properties

### 2. Orchestrator Setup
```python
orchestrator = DeployOrchestrator(context)
```
- Creates orchestrator with context
- Initializes thread pools for parallel operations
- Prepares deployment coordination

### 3. Pre-Installation Steps
```python
orchestrator.execute_pre_installation_steps()
```
- Calls polymorphic `cli_obj.pre_installation_steps(context, threads_dict)`
- Handles multi-NOS scenarios if target CLI type is specified
- Manages background processes and threading

### 4. Installation Phase
```python
orchestrator.execute_installation()
```
- Calls polymorphic `cli_obj.deploy_image_steps(context, **params)`
- Handles image deployment specific to each CLI type
- Manages serial logging and progress tracking

### 5. Post-Installation Steps
```python
orchestrator.execute_post_installation_steps()
```
- Calls polymorphic `cli_obj.post_installation_steps(context)`
- Handles CLI-specific post-installation tasks
- Manages upgrades, reboots, and validation

### Post-Installation-Only Mode
Use `--post_install_only` after an image is installed manually through ONIE. The same
`test_deploy_and_upgrade.py` test then:

1. Completes first boot for every DUT through its NOS-specific CLI implementation.
2. Waits for services to settle.
3. Runs the standard polymorphic post-installation flow.

This mode skips pre-installation and image deployment. It supports SONiC, NVOS, Cumulus,
and DVS through the existing CLI abstractions. Keep the normal NOS-specific arguments:
NVOS and DVS post-installation flows require `--target-version`, while Cumulus and SONiC
can run this mode without an image argument.

```bash
pytest ngts/scripts/sonic_deploy/test_deploy_and_upgrade.py \
  --setup_name=<setup> \
  --sonic-topo=ptf-any \
  --post_install_only
```

### 6. Cleanup
```python
DeployConnectionHelper.handle_serial_log_analyzers(context.serial_log_analyzers)
```
- Cleans up serial log analyzers
- Handles resource cleanup
- Ensures proper termination

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    test_deploy_and_upgrade.py               │
│                     (Orchestration Layer)                   │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                 DeploymentContext                           │
│              (Parameter Management)                         │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                DeployOrchestrator                           │
│               (Flow Coordination)                           │
└─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┘
      │     │     │     │     │     │     │     │     │
      ▼     ▼     ▼     ▼     ▼     ▼     ▼     ▼     ▼
┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
│ Deploy  │ │ Deploy  │ │ Deploy  │ │ Deploy  │ │ Deploy  │
│ Image   │ │Topology │ │Connect  │ │MultiNos │ │   Dpu   │
│ Helper  │ │ Helper  │ │ Helper  │ │ Helper  │ │ Helper  │
└─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘
      │           │           │           │           │
      └───────────┼───────────┼───────────┼───────────┘
                  │           │           │
                  ▼           ▼           ▼
┌─────────────────────────────────────────────────────────────┐
│                  CLI Classes (Polymorphic)                  │
├─────────────────┬─────────────────┬─────────────────────────┤
│ SonicGeneralCli │  NvueGeneralCli │ CumulusGeneralCli │ ... │
│ Default         │                 │                   │     │
└─────────────────┴─────────────────┴─────────────────────────┘
```


## Polymorphic CLI Implementation

### CLI Method Interface

Each CLI class implements three key methods:

#### pre_installation_steps(context, threads_dict)
- Performs CLI-specific pre-installation tasks
- Manages background threads for parallel operations
- Handles setup preparation

#### deploy_image_steps(context, **deployment_params)
- Executes CLI-specific image deployment
- Manages installation process
- Handles CLI-specific deployment logic

#### post_installation_steps(context)
- Performs CLI-specific post-installation tasks
- Handles upgrades and validation
- Manages CLI-specific cleanup

### CLI Type Implementations

#### SONiC (SonicGeneralCliDefault)
- **Pre-installation**: Community setup recovery, PTF docker preparation
- **Installation**: SONiC image deployment with specific parameters
- **Post-installation**: OS upgrade flag setting, reboot-cause cleanup, IPv6 DNS configuration

#### NVUE (NvueGeneralCli)
- **Pre-installation**: NVUE-specific preparation
- **Installation**: NVOS image deployment with device-specific handling
- **Post-installation**: NVUE-specific validation and setup

#### Cumulus (CumulusGeneralCli)
- **Pre-installation**: Cumulus-specific preparation with version handling
- **Installation**: Cumulus image deployment
- **Post-installation**: Cumulus-specific validation

#### DVS (DvsGeneralCli)
- **Pre-installation**: DVS-specific setup preparation
- **Installation**: DVS image deployment
- **Post-installation**: DVS-specific validation

## Error Handling and Recovery

### Exception Management
- All deployment phases wrapped in try-catch blocks
- Specific error handling for each deployment stage
- Graceful degradation for non-critical failures

### Resource Cleanup
- Serial log analyzers properly terminated
- Background threads cleaned up
- Temporary files and resources released

### Recovery Mechanisms
- Reboot recovery options available
- Background process timeout handling
- Deployment rollback capabilities where applicable

## Threading and Parallel Operations

### Background Process Management
- Pre-installation steps run in parallel threads
- Installation threads managed by orchestrator
- Timeout handling for long-running operations

### Thread Coordination
```python
self.pre_install_threads = {}  # Pre-installation background tasks
self.install_threads = []      # Installation background tasks
```

### Synchronization
- `wait_until_deploy_background_process()` ensures completion
- Timeout mechanisms prevent hanging operations
- Proper thread cleanup and resource management

## Configuration and Parameters

### Context Properties
- All deployment parameters accessible through context
- Consistent naming and access patterns
- Type safety and validation

### Setup Information
- Topology details extracted and cached
- DUT information readily available
- CLI objects properly initialized

### Version Management
- Base and target version handling
- Version compatibility checking
- Upgrade path validation

## Benefits of the New Architecture

### Maintainability
- Clear separation of concerns
- Single responsibility for each component
- Easy to locate and fix issues

### Extensibility
- Adding new CLI types requires minimal changes
- Polymorphic design eliminates conditional complexity
- Helper classes can be extended independently

### Testability
- Each component can be unit tested
- Isolated functionality enables focused testing
- Mock objects can be easily substituted

### Readability
- Main deployment function reduced from 500+ to ~50 lines
- Clear flow and responsibility assignment
- Self-documenting code structure

### Performance
- Parallel execution where appropriate
- Efficient resource utilization
- Minimal overhead from abstraction

## Migration Notes

### Backward Compatibility
- All existing functionality preserved
- No breaking changes to external interfaces
- Gradual migration path available

### Parameter Access
- Old direct parameter access replaced with context properties
- Consistent access patterns throughout codebase
- Type safety improvements

### Import Management
- Circular dependencies resolved with lazy imports
- Clean import structure at module level
- Performance impact minimized

## Conclusion

The refactored deployment flow provides a clean, maintainable, and extensible architecture for SONiC deployment operations. The modular design enables easy testing, debugging, and enhancement while maintaining full backward compatibility with existing functionality.
