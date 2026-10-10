"""Unit tests verifying Telegram HTML formatting and link labeling."""

import pytest
from budlance.orchestrator.formatter import to_telegram_html


def test_telegram_html_flight_booking_link_labeled():
    text = (
        "• Transport: IndiGo — INR 8,500.00\n"
        "  🔗 Booking: https://www.google.com/travel/flights?q=DEL+to+MAA&hl=en"
    )
    result = to_telegram_html(text)
    assert '🔗 <a href="https://www.google.com/travel/flights?q=DEL+to+MAA&amp;hl=en">Book flights</a>' in result
    assert "https://www.google.com/travel/flights?q=DEL+to+MAA&hl=en" not in result or "Book flights" in result


from budlance.config import get_settings


def test_telegram_html_booking_relay_relative_url():
    text = (
        "• Transport: Air India — INR 10,200.00\n"
        "  🔗 Booking: /book/abc123def456"
    )
    result = to_telegram_html(text)
    expected_url = f"{get_settings().effective_public_base_url}/book/abc123def456"
    assert f'🔗 <a href="{expected_url}">Book flights</a>' in result


def test_telegram_html_hotel_booking_link_labeled():
    text = (
        "• Accommodation: Munnar Resort (⭐ 4.3 · 320 reviews) — INR 6,000.00\n"
        "  🔗 Booking: https://www.google.com/travel/hotels?q=Munnar+Resort"
    )
    result = to_telegram_html(text)
    assert '🔗 <a href="https://www.google.com/travel/hotels?q=Munnar+Resort">Book hotel</a>' in result


def test_telegram_html_irctc_booking_link_labeled():
    text = (
        "• Transport: Express Train — INR 1,200.00\n"
        "  🔗 Booking: https://www.irctc.co.in/nget/train-search"
    )
    result = to_telegram_html(text)
    assert '🔗 <a href="https://www.irctc.co.in/nget/train-search">Search on IRCTC</a>' in result


def test_telegram_html_attraction_info_link_labeled():
    text = (
        "• 10:00 AM: Visit Tea Museum [₹50]\n"
        "  🔗 Info: https://en.wikipedia.org/wiki/Tea_Museum"
    )
    result = to_telegram_html(text)
    assert '🔗 <a href="https://en.wikipedia.org/wiki/Tea_Museum">View details</a>' in result


def test_telegram_html_escapes_special_characters():
    text = "Budget & Planning: User <Alice> & <Bob> spent $100 *Special Deal*"
    result = to_telegram_html(text)
    assert "Budget &amp; Planning: User &lt;Alice&gt; &amp; &lt;Bob&gt; spent $100 <b>Special Deal</b>" in result


def test_telegram_html_markdown_links_and_formatting():
    text = (
        "Click [Trip Pass](https://example.com/pay) for _discounted_ access `PASS49`."
    )
    result = to_telegram_html(text)
    assert '<a href="https://example.com/pay">Trip Pass</a>' in result
    assert "<i>discounted</i>" in result
    assert "<code>PASS49</code>" in result


def test_telegram_html_no_bare_urls():
    text = "Check out this guide at https://example.com/travel-guide for details."
    result = to_telegram_html(text)
    assert '<a href="https://example.com/travel-guide">Open link</a>' in result
