"""Typed, backend-independent ZLang intermediate-representation package.

IR records are owned by their domain modules. Importing private records from
this package root is intentionally unsupported; compiler subsystems must name
the authoritative owner module.
"""
