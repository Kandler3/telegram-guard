# Native stdio event bridge: isolated prototype

This is an offline transport experiment, not an installed launcher or a production event endpoint.
The installed Telegram Guard, agent-wake, Desktop configuration and launcher remain unchanged.

## Architecture

The bridge starts exactly one real Codex App Server over stdio in a separate test CODEX_HOME.
A simulated Desktop owns initialize, server tool callbacks and approval responses. A local
WebSocket test facade permits only loaded-thread discovery and synthetic turn/start.toolOutput.
Its initialize reply is local facade setup, not a second initialize of the native App Server.
Desktop and event request IDs are remapped independently. Native server requests and notifications
are sent to the Desktop stream. An event producer cannot answer an approval or change configuration.
The prototype refuses a CODEX_HOME outside a .test-runs directory.

## Reproduce

From the project root, run the bundled Node runtime with:

    node tests/probe_native_stdio.mjs ABSOLUTE_CODEX_EXE ABSOLUTE_TEST_PYTHON

Use a Python environment with websockets. All model responses come from an in-process loopback
mock. No OpenAI API key, login, Telegram connection or real Desktop restart is required.
The probe stores synthetic result.json under .test-runs/native-stdio-*/.

Protocol fault coverage:

    python -m pytest tests/test_native_stdio_prototype.py -q

## Verified boundaries

The real native CLI accepted standalone tool output in an idle thread, queued a second event
into an active turn, returned functionCallOutput to its owning client, routed an owner tool
callback, and survived a mock model outage followed by recovery. The event facade rejected user
input and configuration writes. A synthetic subprocess test verified colliding request IDs,
no automatic approval response, explicit Desktop decline forwarding, no event replay after a lost
response and event-side disconnect, and child shutdown after Desktop stdin closes.

## Not verified; do not deploy

Actual Electron Desktop bootstrap and browser/app-tools integration have NOT been tested through
this prototype. The simulated Desktop and synthetic approval fixture are not UI approval QA.
The test WebSocket has no production authentication or trusted producer-to-thread binding.
A production bridge needs authenticated local IPC, bounded pending-request lifetime/size,
backpressure, schema validation, durable event ID receipt handling, protected caller binding,
and rejection of stale session registrations. Existing durable queues should retain their
accepted/rejected/unknown states; the bridge must never replay ambiguous dispatches.
With Desktop closed there is no server; producers must retain events for a later connection.

The installed daemon control endpoint was unavailable during this investigation. That does not
prove daemon mode is unsupported; coexistence with this Desktop build remains unverified.
