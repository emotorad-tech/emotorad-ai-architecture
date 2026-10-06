# Amiigo Chat History API Contract (merged)

Oct 6, 2026 · @Sagnik Mukherjee

This draft is merged into `docs/contracts/amiigo-support-chat.md` (v1), which now covers the real-time chat over a WebSocket, restoring a chat's history with `GET` when the rider comes back, ticket status from Zoho Desk, uploads and deletion in one place. The history endpoints there keep the paths and shapes drafted here, with two changes: a chat's ticket is a `ticket` object (reference, status and closed time) instead of `ticket_reference`, and the messages endpoint also takes `after=<message id>` to fetch what arrived while the socket was closed.
