"""Automated escalation for unacknowledged critical notifications."""

from app.core.database import AsyncSessionLocal


async def escalation_check_job():
    """Re-notify responders with unacknowledged critical alerts past the timeout."""
    from app.crud.system_settings import system_settings_crud
    from app.crud.notification_delivery import notification_delivery_crud
    from app.crud.location import location_crud
    from app.services import notification_service
    from app.schemas.subscription import SendNotificationSchema, CustomNotificationPayload
    from app.models.notification_template import NotificationType

    try:
        async with AsyncSessionLocal() as db:
            timeout = await system_settings_crud.get_value(db, "escalation_timeout_minutes")
            max_escalation = await system_settings_crud.get_value(db, "max_escalation_count")

            if not timeout or not max_escalation:
                return

            timeout = int(timeout)
            max_escalation = int(max_escalation)

            overdue = await notification_delivery_crud.get_unacknowledged_critical_past_threshold(
                db=db,
                timeout_minutes=timeout,
                max_escalation=max_escalation,
            )

            if not overdue:
                return

            # Group overdue deliveries by their dispatch's (site, tier): a
            # site-scoped dispatch escalates only to that site's responders, a
            # NULL-site dispatch to everyone, and the escalation ping keeps the
            # source tier so an ALARM still renders the takeover UI.
            deliveries_by_group: dict[tuple[int | None, NotificationType], list] = {}
            for delivery in overdue:
                key = (delivery.dispatch.location_id, delivery.dispatch.type)
                deliveries_by_group.setdefault(key, []).append(delivery)

            for (location_id, notif_type), group_deliveries in deliveries_by_group.items():
                # Dedup on (site, tier, responder): a responder with several overdue
                # alerts at the same site gets one ping; the same responder
                # overdue at two different sites still gets one ping per site —
                # different sites are different alerts.
                responder_ids = list(
                    dict.fromkeys(d.responder_id for d in group_deliveries)
                )

                site_name = None
                if location_id is not None:
                    location = await location_crud.get(db=db, id=location_id)
                    site_name = location.name if location else f"site {location_id}"

                label = "alarm" if notif_type == NotificationType.ALARM else "critical alert"
                payload = SendNotificationSchema(
                    responder_ids=responder_ids,
                    template_id=None,
                    custom_notification=CustomNotificationPayload(
                        type=notif_type,
                        title="Alarm - Escalation" if notif_type == NotificationType.ALARM else "Critical Alert - Escalation",
                        message=(
                            f"You have unacknowledged {label}s at {site_name}. "
                            "Please review and respond immediately."
                            if site_name
                            else f"You have unacknowledged {label}s. "
                            "Please review and respond immediately."
                        ),
                    ),
                )
                await notification_service.send_notification_to_subscribers(
                    payload=payload,
                    db=db,
                    system_initiated=True,
                    location_id=location_id,
                )
                print(
                    f"⚡ Escalated {len(group_deliveries)} overdue delivery(ies) "
                    f"to {len(responder_ids)} responder(s) at {site_name or 'all sites'}"
                )

            # Increment escalation count for all overdue deliveries
            for delivery in overdue:
                await notification_delivery_crud.increment_escalation_count(db=db, delivery_id=delivery.id)

            print(f"⚡ Escalation run complete: {len(overdue)} delivery(ies) escalated across {len(deliveries_by_group)} site(s)")

    except Exception as e:
        print(f"⚠️ Escalation check failed: {e}")
