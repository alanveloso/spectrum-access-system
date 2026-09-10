"""Inventory of WInnForum harness SasAdminInterface HTTP paths.

Source: Wireless-Innovation-Forum/Spectrum-Access-System ``sas.py``
(``SasAdminImpl``), Release 1 harness. Paths are relative to AdminApiBaseUrl
and always use POST unless noted.

``EXPLICIT_ROUTED_ADMIN_POST_PATHS`` means a dedicated FastAPI route exists —
not that domain behaviour is complete. Thin/persisted stubs remain until their
phase tasks land; only ``EXPLICIT_UNIMPLEMENTED_*`` may return non-success.
"""

from __future__ import annotations

# Official admin paths used by SasAdminImpl (no leading /admin prefix here —
# FastAPI router already mounts at /admin).
OFFICIAL_ADMIN_POST_PATHS: frozenset[str] = frozenset(
    {
        "reset",
        "injectdata/fcc_id",
        "injectdata/user_id",
        "injectdata/esc_zone",
        "injectdata/exclusion_zone",
        "injectdata/zone",
        "injectdata/pal_database_record",
        "injectdata/cluster_list",
        "injectdata/blacklist_fcc_id",
        "injectdata/blacklist_fcc_id_and_serial_number",
        "injectdata/conditional_registration",
        "injectdata/fss",
        "injectdata/wisp",
        "injectdata/sas_admin",
        "injectdata/esc_sensor",
        "injectdata/cpi_user",
        "injectdata/peer_sas",
        "injectdata/database_url",
        "trigger/esc_detection",
        "trigger/esc_reset",
        "trigger/meas_report_in_registration_response",
        "trigger/meas_report_in_heartbeat_response",
        "trigger/create_ppa",
        "trigger/daily_activities_immediately",
        "trigger/enable_scheduled_daily_activities",
        "trigger/enable_ntia_15_517",
        "trigger/load_dpas",
        "trigger/bulk_dpa_activation",
        "trigger/dpa_activation",
        "trigger/dpa_deactivation",
        "trigger/disconnect_esc",
        "trigger/create_full_activity_dump",
        "get_daily_activities_status",
        "get_ppa_status",
        "query/propagation_and_antenna_model",
    }
)

# Dedicated routes present (may still be thin / partial domain).
EXPLICIT_ROUTED_ADMIN_POST_PATHS: frozenset[str] = frozenset(
    {
        "reset",
        "injectdata/fcc_id",
        "injectdata/user_id",
        "injectdata/esc_zone",
        "injectdata/exclusion_zone",
        "injectdata/zone",
        "injectdata/pal_database_record",
        "injectdata/cluster_list",
        "injectdata/blacklist_fcc_id",
        "injectdata/blacklist_fcc_id_and_serial_number",
        "injectdata/conditional_registration",
        "injectdata/fss",
        "injectdata/wisp",
        "injectdata/sas_admin",
        "injectdata/esc_sensor",
        "injectdata/cpi_user",
        "injectdata/peer_sas",
        "injectdata/database_url",
        "trigger/esc_detection",
        "trigger/esc_reset",
        "trigger/meas_report_in_registration_response",
        "trigger/meas_report_in_heartbeat_response",
        "trigger/create_ppa",
        "trigger/daily_activities_immediately",
        "trigger/enable_scheduled_daily_activities",
        "trigger/enable_ntia_15_517",
        "trigger/load_dpas",
        "trigger/bulk_dpa_activation",
        "trigger/dpa_activation",
        "trigger/dpa_deactivation",
        "trigger/disconnect_esc",
        "trigger/create_full_activity_dump",
        "get_daily_activities_status",
        "get_ppa_status",
        "query/propagation_and_antenna_model",
    }
)

# Official paths with an explicit non-success handler (must not fake PASS).
# Empty after : propagation query is implemented (may still 400/503).
EXPLICIT_UNIMPLEMENTED_ADMIN_POST_PATHS: frozenset[str] = frozenset()


def missing_official_admin_paths() -> frozenset[str]:
    return (
        OFFICIAL_ADMIN_POST_PATHS
        - EXPLICIT_ROUTED_ADMIN_POST_PATHS
        - EXPLICIT_UNIMPLEMENTED_ADMIN_POST_PATHS
    )
