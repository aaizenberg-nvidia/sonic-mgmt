# Baselines directory for the IB telemetry test suite.
#
# Generic (product-independent) files:
#   - aport_schema_baseline.json       : gNMI subtree of one Aport, captured
#                                        on a known-good pre-feature build
#                                        (preferred per Test Plan §7.1 (a)).
#   - aport_schema_baseline.nvue.json  : `nv show interface <aport> -o json`
#                                        of one Aport, captured on a known-
#                                        good pre-feature build (fallback per
#                                        Test Plan §7.1 (c)).
#
# Product-specific overrides (optional): the loader prefers a file whose name
# carries a product key before the extension, falling back to the generic file
# above. The key is the device class name minus 'Switch' (lowercased), then
# asic_type. Examples:
#   - aport_schema_baseline.blackmamba.nvue.json
#   - aport_schema_baseline.crocodile.nvue.json
#   - aport_schema_baseline.qtm3.json
# Only add an override when a product genuinely exposes a different leaf set;
# the QTM3 family shares the NVUE Aport leaf schema, so the generic file
# normally suffices (the schema test compares leaf paths, not values).
#
# Used by ngts/tests_nvos/general/telemetry/ib/test_planeport_data_model.py::
# test_aport_backward_compat_schema_unchanged. Update in the same commit as
# any legitimate schema change.
