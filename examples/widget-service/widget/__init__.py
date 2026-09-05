"""A fictional widget service used by the Bellhop demo."""

WIDGETS = ["anvil", "bobbin", "cog"]


def health() -> dict:
    return {"status": "ok"}


def list_widgets() -> list:
    return list(WIDGETS)
