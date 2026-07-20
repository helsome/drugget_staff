from price_specialist.routing import decide_price_break_route, delivery_idempotency_key


def test_unassigned_store_never_notifies_arbitrary_recipient() -> None:
    route = decide_price_break_route(
        responsible_person=None,
        contact="someone@example.com",
        responsible_unit="华东",
    )
    assert route.routing_status == "unassigned"
    assert route.recipient is None


def test_notification_is_dry_run_until_business_confirmation() -> None:
    route = decide_price_break_route(
        responsible_person="张三",
        contact="zhangsan@example.com",
        responsible_unit="华东",
        dry_run=True,
    )
    assert route.routing_status == "dry_run"
    assert route.dry_run is True
    assert delivery_idempotency_key(event_id="e1", recipient=route.recipient, channel=route.channel) == delivery_idempotency_key(
        event_id="e1", recipient=route.recipient, channel=route.channel
    )

