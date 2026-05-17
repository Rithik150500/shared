"""Integration: PDF media upload → template send.

Per plan §19.2.1. The new_order template carries a document header so
the worker must upload the PDF media first, then dispatch the template
with the resulting media_id. This skeleton mocks both HTTP calls and
asserts the upload-then-send ordering.
"""
from __future__ import annotations

import asyncio

import respx
from httpx import Response

from whatsapp_delivery.dispatch import queue as q
from whatsapp_delivery.dispatch.worker import _do_send_template


_MEDIA_URL = "https://graph.facebook.com/v20.0/111/media"
_MESSAGES_URL = "https://graph.facebook.com/v20.0/111/messages"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@respx.mock
def test_template_with_document_header_uploads_then_sends(
    fake_redis,
    db_session,
):
    """Enqueue a new_order template with PDF bytes; assert two HTTP calls
    in order (upload → send) and the media_id flows through."""
    upload_route = respx.post(_MEDIA_URL).mock(
        return_value=Response(200, json={"id": "media-int-001"}),
    )
    send_route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.media"}]}),
    )

    job_id = _run(
        q.enqueue_send_template(
            to="+919876543210",
            template_name="nowlez_new_order_v1",
            language="en_US",
            variables={
                "case_title": "Mehta v. State",
                "order_date": "01 Jan 2026",
                "order_descriptive_name": "Final Order",
                "case_id": "case-uuid-int",
            },
            brand="nowlez",
            media_bytes=b"%PDF-1.4\n...integration-bytes",
            media_filename="order.pdf",
            media_mime="application/pdf",
            related_case_id="case-uuid-int",
        ),
    )
    assert job_id

    # Worker executes — direct invocation with the queued kwargs.
    from rq import Queue
    job = Queue("whatsapp_send", connection=fake_redis).jobs[0]
    wamid = _do_send_template(**job.kwargs)
    assert wamid == "wamid.media"

    # Upload must have happened exactly once and BEFORE the send.
    assert upload_route.call_count == 1
    assert send_route.call_count == 1

    # The send payload must reference the media-id returned by the upload.
    import json
    send_body = json.loads(send_route.calls.last.request.content.decode())
    components = send_body["template"]["components"]
    header = next(c for c in components if c["type"] == "header")
    assert header["parameters"][0]["document"] == {"id": "media-int-001"}
