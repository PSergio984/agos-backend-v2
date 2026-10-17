"""#28 — the 'maintenance' notification preference.

The API's NotifPreferenceKey Literal has always accepted "maintenance", but
NotificationPreference had no field for it — model_copy(update=...) set the
attribute on the copy and model_dump() silently dropped it on persist. The
field now exists (default True so pre-migration rows hydrate).
"""

from app.models.responder_related.responders import NotificationPreference


def test_maintenance_key_persists_through_copy_and_dump():
    prefs = NotificationPreference(
        warning=True, critical=True, blockage=True, announcement=True
    )
    updated = prefs.model_copy(update={"maintenance": False})
    assert updated.model_dump()["maintenance"] is False


def test_pre_migration_rows_hydrate_with_maintenance_true():
    # Rows written before the field existed carry only the four old keys.
    prefs = NotificationPreference(
        **{"warning": True, "critical": True, "blockage": True, "announcement": True}
    )
    assert prefs.maintenance is True
