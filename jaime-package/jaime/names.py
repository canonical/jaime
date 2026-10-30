"""Juju unit-name helpers shared by both charms."""


def app_of(unit_name: str) -> str:
    """The application a unit belongs to (``postgresql/0`` -> ``postgresql``)."""
    return unit_name.split("/")[0]


def unit_tag(unit_name: str) -> str:
    """The Juju agent tag of a unit (``postgresql/0`` -> ``unit-postgresql-0``)."""
    return "unit-" + unit_name.replace("/", "-")
