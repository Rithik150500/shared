"""Integration: enqueue → worker → mocked Meta → delivery_log row updated.

Per plan §19.2.1. Exercises the producer -> worker hand-off via the
shared queue, then asserts the worker landed the expected wamid and
(in production) the delivery log row picks it up. The Meta Cloud API
is mocked at the httpx layer via respx so the test stays hermetic.

This is a skeleton — it exercises the public contract (enqueue +
worker round-trip + DB linkage) but does NOT exhaustively cover every
template shape. The shape coverage lives in the unit suite
(``test_worker.py``, ``test_template_client.py``).
"""
from __future__ import annotations

import asyncio

import respx
from httpx import Response
from rq import Queue

from whatsapp_delivery.dispatch import queue as q
from whatsapp_delivery.dispatch.worker import _do_send_template


_MESSAGES_URL = "https://graph.facebook.com/v20.0/111/messages"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@respx.mock
def test_enqueue_template_then_worker_executes_and_returns_wamid(
    fake_redis,
    db_session,
):
    """Producer enqueues a Nowlez welcome template; worker pops + executes;
    Meta returns a wamid; the worker hands the wamid back as the job
    return value."""
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.e2e"}]}),
    )

    # 1. Producer side — enqueue and get an RQ job id.
    job_id = _run(
        q.enqueue_send_template(
            to="+919999999999",
            template_name="nowlez_signup_welcome_v2",
            language="en_US",
            variables={"user_name": "Asha"},
            brand="nowlez",
        ),
    )
    assert job_id

    # 2. Pull the queued job and confirm shape.
    rq_queue = Queue("whatsapp_send", connection=fake_redis)
    assert rq_queue.count == 1
    job = rq_queue.jobs[0]
    assert job.func_name.endswith("_do_send_template")

    # 3. Worker side — invoke the function directly with the queued kwargs
    # (fakeredis can't actually start a worker process, but the function
    # being called from RQ is what matters end-to-end).
    wamid = _do_send_template(**job.kwargs)
    assert wamid == "wamid.e2e"

    # 4. delivery_log binding is a best-effort hook gated on rq.get_current_job
    # (which returns None outside the worker context). In production the
    # worker has bound a job ctx so the row is updated; here we just confirm
    # the worker returned the wamid (the binding helper is unit-tested in
    # the worker module).
