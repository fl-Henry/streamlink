from pathlib import Path


def core() -> str:
    """
    Read the contents of the JavaScript core solvers bundle as string.
    """
    return (Path(__file__).parent / "yt.solver.core.js").read_text(encoding="utf-8")


def lib() -> str:
    """
    Read the contents of the JavaScript library solvers bundle as string.
    """
    return (Path(__file__).parent / "yt.solver.lib.js").read_text(encoding="utf-8")
