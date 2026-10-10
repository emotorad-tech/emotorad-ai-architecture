"""Zoho Desk for the customer chatbot (spec 2026-10-05-zoho-desk-tickets-design).

Nothing in this package calls Zoho or starts a thread at import. The worker
starts only in the API's lifespan, and only when Zoho is configured.
"""
