"""Display names for suppliers, shared by API responses (the web app mirrors this list)."""

SUPPLIER_LABELS: dict[str, str] = {
    # v1 accepts only duffel_test_ tokens, so Duffel is always the sandbox.
    "duffel": "Duffel (sandbox)",
    "sim_gds": "Simulated GDS",
    "sim_consolidator": "Simulated consolidator",
    "sim_ota": "Simulated OTA",
    "demo": "Demo supplier",
}


def supplier_label(supplier_id: str) -> str:
    return SUPPLIER_LABELS.get(supplier_id, supplier_id)
