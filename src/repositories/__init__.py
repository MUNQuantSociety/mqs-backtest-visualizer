"""Repositories — the only layer that writes SQL.

Services call these; routes never do. Keeping every query in one place is what
makes the owner-scoping seam (``for_user``) a single edit when auth lands.
"""
